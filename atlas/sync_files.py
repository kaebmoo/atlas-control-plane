"""Shared, safe-by-construction handling of worker-supplied files moved over the thClaws
`/workspace/sync/*` surface (T5 collection, T6 handoff). A worker is only semi-trusted: a
compromised or buggy one can hand Atlas a hostile gzip tar (path traversal, symlink escape,
device nodes, or a decompression bomb). Every ingestion path MUST go through
`safe_extract_tar` — it is the single validator, so the caps and member filter can never be
bypassed by a second, laxer reader.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import tarfile
from pathlib import Path, PurePosixPath

from .db import new_id


class SyncFileError(ValueError):
    """A worker tar violated the member filter or a cap. A ValueError subclass so the fuzz
    gate's "validators raise only ValueError" rule holds, and so the collection barrier's
    failure isolation catches it like any other collection failure."""


def _reject_unsafe_path(name: str) -> str:
    """Return the normalized POSIX relpath of a tar member, or raise. A member name is safe
    ONLY when it is a plain relative path with no traversal — the checks below are each a
    distinct escape vector, kept as separate statements so a single one can be mutation-tested."""
    if not name or not name.strip():
        raise SyncFileError("tar member has an empty name")
    # Backslashes are a Windows-separator smuggling trick; forbid them outright rather than
    # guess intent. PurePosixPath treats them as a normal char, so the '..' check would miss
    # `..\\evil`.
    if "\\" in name:
        raise SyncFileError(f"tar member name contains a backslash: {name!r}")
    pure = PurePosixPath(name)
    if pure.is_absolute() or name.startswith("/"):
        raise SyncFileError(f"tar member has an absolute path: {name!r}")
    if ".." in pure.parts:
        raise SyncFileError(f"tar member escapes with '..': {name!r}")
    return pure.as_posix()


def safe_extract_tar(
    raw: bytes,
    *,
    max_files: int,
    max_bytes: int,
) -> list[tuple[str, bytes]]:
    """Validate a worker-supplied gzip tar and return `[(relpath, content), …]` for its
    regular-file members — content only, never written to a path derived from a member name.

    Rejects (SyncFileError): absolute / `..` / backslash paths, and any non-regular member
    (symlink, hardlink, device, fifo) — those are the traversal/escape vectors. Directory
    members are skipped, not stored. Enforces the file-count and total-UNCOMPRESSED-byte caps
    while streaming, so a decompression bomb aborts before its bytes are fully realized (the
    declared member sizes drive the tar layout, so summing them bounds the real read)."""
    try:
        tar = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise SyncFileError(f"worker tar could not be opened: {exc}") from exc

    out: list[tuple[str, bytes]] = []
    total = 0
    count = 0
    try:
        # Iteration and per-member reads decompress lazily, so a tar that OPENS cleanly can
        # still raise TarError/EOFError partway through (a truncated or corrupt-mid-stream
        # archive from a buggy/compromised worker). Catch those here and re-raise as
        # SyncFileError, so this validator's contract is "raises only SyncFileError" — no raw
        # tarfile/OS exception ever leaks to a caller (the failure-isolated collection barrier,
        # or any future tar ingestion, then catches one known type).
        for member in tar:
            if member.isdir():
                continue
            if not member.isreg():
                # symlink/hardlink/chardev/blockdev/fifo — every one is an escape or a
                # resource-abuse vector, none is benign file content. Reject the whole tar.
                raise SyncFileError(f"tar member is not a regular file: {member.name!r}")
            relpath = _reject_unsafe_path(member.name)
            count += 1
            if count > max_files:
                raise SyncFileError(f"tar exceeds the {max_files}-file cap")
            size = member.size if member.size and member.size > 0 else 0
            total += size
            if total > max_bytes:
                raise SyncFileError(f"tar exceeds the {max_bytes}-byte cap")
            handle = tar.extractfile(member)
            data = handle.read(size) if handle is not None else b""
            out.append((relpath, data))
    except SyncFileError:
        raise
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise SyncFileError(f"worker tar could not be read: {exc}") from exc
    finally:
        tar.close()
    return out


def store_bytes(upload_dir: Path, data: bytes) -> tuple[str, str]:
    """Atomically write `data` to the upload store under a fresh OPAQUE id (never a
    caller/worker-supplied name), fsync, and return `(opaque_id, sha256_hex)`. Same
    temp-then-rename discipline as the dashboard upload path, so a crash never leaves a
    partial file the id points at."""
    opaque_id = new_id("file")
    target = upload_dir / opaque_id
    temporary = upload_dir / f".{opaque_id}.tmp"
    digest = hashlib.sha256(data).hexdigest()
    try:
        with temporary.open("xb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        raise
    return opaque_id, digest


def build_push_tar(files: list[tuple[str, bytes]]) -> bytes:
    """Assemble a REPRODUCIBLE gzip tar from `[(arcname, content), …]` for a T6 push:
    deterministic member order (sorted by arcname), normalized mtime/ids/mode, and a gzip
    header with mtime=0 — so the same file set hashes to the same bytes for audit. The tar is
    written straight through a `GzipFile(mtime=0)` because `tarfile`'s `w:gz` bakes the
    current time into the gzip header, which would break reproducibility — streaming members
    into the compressor also avoids materializing the whole uncompressed tar as a second
    in-memory copy. Callers set arcnames (Atlas controls the target layout —
    `incoming/<run_id>/<node_key>/…`), never the worker."""
    gz_buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=gz_buffer, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as tar:
        for arcname, data in sorted(files, key=lambda item: item[0]):
            info = tarfile.TarInfo(name=arcname)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    return gz_buffer.getvalue()
