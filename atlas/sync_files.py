"""Shared upload-store and path-safety primitives used by both the current T9a Job Artifact
collection path (atlas/jobs.py) and T9b file handoff (atlas/workflows.py). A worker is only
semi-trusted, so every relpath it reports MUST go through `_reject_unsafe_path` before it is
used to build a key or destination path.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath

from .db import new_id


class SyncFileError(ValueError):
    """A worker-reported path violated the member filter. A ValueError subclass so the fuzz
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


# build_push_tar (T6's reproducible push tar) was deleted with its last caller: T9b hands
# files off through Bearer-authenticated POST /v1/inputs (workflows._push_files_to_worker).
