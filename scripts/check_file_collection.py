"""T5 — selective file collection via /workspace/sync/export. Hermetic checks (own temp DB,
ephemeral port, mock thClaws worker) for the pre-terminal collection barrier: the safe tar
extractor, the byte/file caps, the sync_mode gate, 409-retry, deadline bounding, and the
barrier ordering vs handoff.

Mutation targets (break the code -> this file goes red):
- disable the '..' rejection in sync_files._reject_unsafe_path -> the hostile-tar test flips
  from collection_failed to collected.
- move the barrier AFTER the succeeded write -> the ordering test sees handoff before collect.
- probe export even when sync_mode == 'disabled' -> export_calls > 0 on the disabled worker.
- revert the mid-iteration guard in safe_extract_tar -> a corrupt tar's raw TarError escapes
  and flips the job to failed.
- narrow _collect_files's broad except back to a typed tuple -> a sqlite3 error from the
  artifact write escapes and flips the job to failed.
"""

from __future__ import annotations

import gzip
import io
import sqlite3
import sys
import tarfile
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.app import AtlasHttpServer, AtlasRuntime
from atlas.config import Config
from scripts.check_lib import request_json


def _gzip_tar(members: list[tuple[str, bytes, str]]) -> bytes:
    """members: (name, data, kind) where kind is 'file' or 'symlink'."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class MockWorker(BaseHTTPRequestHandler):
    # Test-scriptable behaviour (class attributes, flipped between cases).
    export_calls = 0
    export_status = 200
    export_409_countdown = 0  # return 409 this many times, then 200
    export_delay = 0.0
    export_tar = b""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send_json(self, payload: object) -> None:
        import json

        body = json.dumps(payload).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send_json({"ok": True})
        elif self.path == "/v1/agent/info":
            self._send_json({"version": "0.85.0"})
        elif self.path == "/v1/models":
            self._send_json({"object": "list", "data": []})
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        cls = type(self)
        length = int(self.headers.get("Content-Length") or 0)
        _ = self.rfile.read(length) if length else b""
        if self.path == "/agent/run":
            # A minimal successful stream: one text frame, then [DONE].
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'event: text\ndata: {"text": "collected work"}\n\n')
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        if self.path == "/workspace/sync/export":
            cls.export_calls += 1
            if cls.export_delay:
                time.sleep(cls.export_delay)
            if cls.export_409_countdown > 0:
                cls.export_409_countdown -= 1
                self.send_error(HTTPStatus.CONFLICT)
                return
            if cls.export_status != 200:
                self.send_error(cls.export_status)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Length", str(len(cls.export_tar)))
            self.end_headers()
            self.wfile.write(cls.export_tar)
            return
        self.send_error(HTTPStatus.NOT_FOUND)


def _wait_terminal(runtime: AtlasRuntime, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = runtime.db.get_job(job_id)
        if job and job["state"] in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never reached a terminal state")


def _events(runtime: AtlasRuntime, job_id: str) -> list[dict]:
    return runtime.db.get_job_events_after(job_id, 0, limit=1000)


def _event_types(runtime: AtlasRuntime, job_id: str) -> list[str]:
    return [event["event_type"] for event in _events(runtime, job_id)]


def _reset_worker(cls: type[MockWorker]) -> None:
    cls.export_calls = 0
    cls.export_status = 200
    cls.export_409_countdown = 0
    cls.export_delay = 0.0


def _submit(runtime: AtlasRuntime, worker_id: str, collect_files, extra: dict | None = None) -> dict:
    payload = {"prompt": "do work", "worker_id": worker_id, "allowed_worker_ids": [worker_id]}
    if collect_files is not None:
        payload["collect_files"] = collect_files
    if extra:
        payload.update(extra)
    return runtime.jobs.submit(payload)


def check_happy_path(runtime: AtlasRuntime, worker_id: str) -> None:
    import hashlib

    _reset_worker(MockWorker)
    a = b"# report\nhello"
    b = b"1,2,3\n4,5,6"
    MockWorker.export_tar = _gzip_tar([("reports/a.md", a, "file"), ("out/data.csv", b, "file")])
    job = _submit(runtime, worker_id, ["reports/a.md", "out/data.csv"])
    job = _wait_terminal(runtime, job["id"])
    assert job["state"] == "succeeded", job["state"]
    assert MockWorker.export_calls == 1, MockWorker.export_calls
    assert "files.collected" in _event_types(runtime, job["id"])

    arts = [art for art in runtime.db.list_artifacts(limit=1000) if (art.get("metadata") or {}).get("source_job_id") == job["id"]]
    assert len(arts) == 2, arts
    # The per-job artifacts route (GET /api/jobs/{id}/artifacts, dashboard T5 gap) filters by
    # job_id — lock that query so a standalone job's collected files are retrievable by job.
    assert len(runtime.db.list_artifacts(job_id=job["id"])) == 2, "collected files must be listable by job_id"
    by_relpath = {art["metadata"]["relpath"]: art for art in arts}
    for relpath, data in (("reports/a.md", a), ("out/data.csv", b)):
        art = by_relpath[relpath]
        assert art["kind"] == "file_ref"
        assert art["key"] == f"files.{relpath}", art["key"]
        assert art["metadata"]["sha256"] == hashlib.sha256(data).hexdigest()
        # downloaded bytes byte-identical (read the opaque-id file straight from the store).
        stored = (runtime.upload_dir / art["content"]).read_bytes()
        assert stored == data, relpath
    print("  happy path OK: 2 artifacts, byte-identical, correct sha256")


def check_no_config_no_calls(runtime: AtlasRuntime, worker_id: str) -> None:
    _reset_worker(MockWorker)
    job = _submit(runtime, worker_id, None)
    job = _wait_terminal(runtime, job["id"])
    assert job["state"] == "succeeded"
    assert MockWorker.export_calls == 0, "no collect_files must make ZERO sync calls"
    types = _event_types(runtime, job["id"])
    assert "files.collected" not in types and "files.collection_skipped" not in types, types
    print("  no config -> zero sync calls OK")


def check_disabled_skips(runtime: AtlasRuntime, worker_id: str) -> None:
    _reset_worker(MockWorker)
    runtime.db.set_worker_sync_mode(worker_id, "disabled")
    job = _submit(runtime, worker_id, ["reports/a.md"])
    job = _wait_terminal(runtime, job["id"])
    assert job["state"] == "succeeded"
    assert MockWorker.export_calls == 0, "disabled worker must not be called"
    assert "files.collection_skipped" in _event_types(runtime, job["id"])
    runtime.db.set_worker_sync_mode(worker_id, "tunnel")
    print("  sync_mode=disabled -> skipped, no network call OK")


def check_hostile_tar(runtime: AtlasRuntime, worker_id: str) -> None:
    _reset_worker(MockWorker)
    # A '..' traversal member alongside a benign one. With the '..' rejection ON the whole
    # collection fails; disabling it (mutation) flips this to files.collected -> red.
    MockWorker.export_tar = _gzip_tar([("../evil", b"pwned", "file"), ("ok.txt", b"fine", "file")])
    job = _submit(runtime, worker_id, ["ok.txt"])
    job = _wait_terminal(runtime, job["id"])
    assert job["state"] == "succeeded", "collection failure must NOT change the job outcome"
    types = _event_types(runtime, job["id"])
    assert "files.collection_failed" in types, types
    assert "files.collected" not in types, "hostile tar must be rejected, not collected"
    # nothing escaped: no artifact carries the traversal name.
    escaped = [
        art for art in runtime.db.list_artifacts(limit=1000)
        if "evil" in str((art.get("metadata") or {}).get("relpath") or "")
    ]
    assert not escaped, escaped
    print("  hostile tar rejected; job still succeeded OK")


def check_symlink_rejected(runtime: AtlasRuntime, worker_id: str) -> None:
    _reset_worker(MockWorker)
    MockWorker.export_tar = _gzip_tar([("link", b"", "symlink")])
    job = _submit(runtime, worker_id, ["link"])
    job = _wait_terminal(runtime, job["id"])
    assert job["state"] == "succeeded"
    assert "files.collection_failed" in _event_types(runtime, job["id"])
    print("  symlink member rejected OK")


def check_caps_abort(runtime: AtlasRuntime, worker_id: str) -> None:
    # The RESPONSE-tar byte cap (the request path count is within the default cap, so submit
    # accepts; the oversized tar aborts extraction). Bounds a decompression bomb too.
    _reset_worker(MockWorker)
    original = runtime.jobs.sync_max_bytes
    runtime.jobs.sync_max_bytes = 3
    try:
        MockWorker.export_tar = _gzip_tar([("a.txt", b"xxxxx", "file"), ("b.txt", b"yyyyy", "file")])
        job = _submit(runtime, worker_id, ["a.txt", "b.txt"])
        job = _wait_terminal(runtime, job["id"])
        assert job["state"] == "succeeded"
        assert "files.collection_failed" in _event_types(runtime, job["id"])
        audits = [row for row in runtime.db.list_audit() if row["action"] == "files.collection_failed"]
        assert audits, "collection failure must be audited"
    finally:
        runtime.jobs.sync_max_bytes = original
    print("  caps abort collection; job stays succeeded; failure audited OK")


def check_409_then_success(runtime: AtlasRuntime, worker_id: str) -> None:
    _reset_worker(MockWorker)
    MockWorker.export_409_countdown = 2  # two 409s, then the tar
    MockWorker.export_tar = _gzip_tar([("r.md", b"ret--y", "file")])
    job = _submit(runtime, worker_id, ["r.md"])
    job = _wait_terminal(runtime, job["id"])
    assert job["state"] == "succeeded"
    assert MockWorker.export_calls == 3, MockWorker.export_calls
    assert "files.collected" in _event_types(runtime, job["id"])
    print("  409-then-success: bounded retry completes OK")


def check_persistent_409_gives_up(runtime: AtlasRuntime, worker_id: str) -> None:
    _reset_worker(MockWorker)
    MockWorker.export_status = HTTPStatus.CONFLICT
    job = _submit(runtime, worker_id, ["r.md"])
    job = _wait_terminal(runtime, job["id"])
    assert job["state"] == "succeeded"
    assert "files.collection_failed" in _event_types(runtime, job["id"])
    assert MockWorker.export_calls >= 2, "must have retried at least once before giving up"
    print("  persistent 409 -> bounded give-up OK")


def check_deadline(runtime: AtlasRuntime, worker_id: str) -> None:
    _reset_worker(MockWorker)
    original = runtime.jobs.collect_deadline_seconds
    runtime.jobs.collect_deadline_seconds = 0.05
    MockWorker.export_delay = 0.6  # far beyond the deadline -> request times out
    MockWorker.export_tar = _gzip_tar([("r.md", b"slow", "file")])
    try:
        job = _submit(runtime, worker_id, ["r.md"])
        job = _wait_terminal(runtime, job["id"])
        assert job["state"] == "succeeded", "deadline must not change the job outcome"
        assert "files.collection_failed" in _event_types(runtime, job["id"])
    finally:
        runtime.jobs.collect_deadline_seconds = original
        MockWorker.export_delay = 0.0
    print("  deadline exceeded -> collection_failed, job still succeeds OK")


def check_corrupt_tar(runtime: AtlasRuntime, worker_id: str) -> None:
    # A tar that OPENS cleanly (first header intact) but is truncated mid-stream — a buggy or
    # compromised worker, or a partial write. Raises tarfile.ReadError DURING iteration, not at
    # open(). The extractor must convert that to SyncFileError (unit assert), and the job must
    # still reach succeeded (failure isolation). Mutation: revert the mid-iteration guard in
    # safe_extract_tar -> the raw TarError escapes -> the job flips to failed -> red.
    from atlas.sync_files import SyncFileError, safe_extract_tar

    raw_tar = io.BytesIO()
    with tarfile.open(fileobj=raw_tar, mode="w") as tar:  # uncompressed, so we can truncate it
        for name in ("big1.bin", "big2.bin"):
            info = tarfile.TarInfo(name)
            info.size = 4096
            tar.addfile(info, io.BytesIO(b"A" * 4096))
    truncated = raw_tar.getvalue()[: 512 + 2048]  # first header + partial first member
    corrupt = gzip.compress(truncated)

    try:
        safe_extract_tar(corrupt, max_files=200, max_bytes=1 << 20)
        raise AssertionError("corrupt tar must raise")
    except SyncFileError:
        pass  # the extractor's contract: only SyncFileError, never a raw tarfile error

    _reset_worker(MockWorker)
    MockWorker.export_tar = corrupt
    job = _submit(runtime, worker_id, ["big1.bin"])
    job = _wait_terminal(runtime, job["id"])
    assert job["state"] == "succeeded", "a corrupt worker tar must not fail the job"
    assert "files.collection_failed" in _event_types(runtime, job["id"])
    print("  corrupt mid-stream tar -> SyncFileError, job still succeeds OK")


def check_db_error_isolated(runtime: AtlasRuntime, worker_id: str) -> None:
    # A sqlite3 error from the artifact write (disk-full / lock exhaustion / corruption) must
    # NOT flip the job to failed — the collection barrier's broad catch owns it. Mutation:
    # narrow _collect_files's except back to a typed tuple -> sqlite3.Error escapes -> red.
    _reset_worker(MockWorker)
    MockWorker.export_tar = _gzip_tar([("r.md", b"content", "file")])
    original = runtime.db.create_artifact

    def boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    runtime.db.create_artifact = boom  # type: ignore[method-assign]
    try:
        job = _submit(runtime, worker_id, ["r.md"])
        job = _wait_terminal(runtime, job["id"])
        assert job["state"] == "succeeded", "a DB error during collection must not fail the job"
        assert "files.collection_failed" in _event_types(runtime, job["id"])
    finally:
        runtime.db.create_artifact = original  # type: ignore[method-assign]
    print("  DB error during collection isolated; job still succeeds OK")


def check_cancel_during_collection(runtime: AtlasRuntime, worker_id: str, handoff_worker_id: str) -> None:
    # A cancel landing while the collection barrier blocks must win: the job ends cancelled
    # and no handoff starts (mutation: drop the post-barrier is_cancel_requested re-check in
    # _run -> the succeeded write overwrites the cancel and handoff fires -> red).
    _reset_worker(MockWorker)
    MockWorker.export_tar = _gzip_tar([("out.md", b"deliverable", "file")])
    MockWorker.export_delay = 1.0
    job = runtime.jobs.submit(
        {
            "prompt": "do work",
            "worker_id": worker_id,
            "allowed_worker_ids": [worker_id],
            "collect_files": ["out.md"],
            "handoff": {"enabled": True, "worker_id": handoff_worker_id},
        }
    )
    # Wait until the export call is in flight — the pre-barrier cancel check has already passed.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and MockWorker.export_calls == 0:
        time.sleep(0.02)
    assert MockWorker.export_calls == 1, "collection never started"
    runtime.db.mark_cancel_requested(job["id"])
    job = _wait_terminal(runtime, job["id"])
    assert job["state"] == "cancelled", f"cancel during collection must win, got {job['state']}"
    assert not job.get("handoff_job_id"), "a cancelled job must not hand off"
    assert "handoff_started" not in _event_types(runtime, job["id"])
    print("  cancel during collection wins; no handoff OK")


def check_job_artifacts_route(runtime: AtlasRuntime, base_url: str, worker_id: str, token: str) -> None:
    # T5 dashboard gap: a standalone job's collected files are keyed to the JOB (run_id NULL),
    # so GET /api/jobs/{id}/artifacts must return them for the Jobs-view download list. End-to-end
    # over HTTP (a static substring can't tell a working route from a broken one — mutation:
    # break the route's len(parts)/job_id filter -> this goes red).
    _reset_worker(MockWorker)
    MockWorker.export_tar = _gzip_tar([("out/report.md", b"job-scoped file", "file")])
    job = _submit(runtime, worker_id, ["out/report.md"])
    job = _wait_terminal(runtime, job["id"])
    assert job["state"] == "succeeded"
    status, body, _ = request_json(base_url, "GET", f"/api/jobs/{job['id']}/artifacts", None, token)
    assert status == 200, (status, body)
    files = [art for art in body["artifacts"] if art.get("kind") == "file_ref"]
    assert len(files) == 1 and files[0]["key"] == "files.out/report.md", files
    # an unknown job id is a clean 404, not a 500.
    status, _, _ = request_json(base_url, "GET", "/api/jobs/job_missing/artifacts", None, token)
    assert status == 404, status
    print("  GET /api/jobs/{id}/artifacts returns job-scoped collected files OK")


def check_barrier_ordering(runtime: AtlasRuntime, worker_id: str, handoff_worker_id: str) -> None:
    _reset_worker(MockWorker)
    MockWorker.export_tar = _gzip_tar([("out.md", b"deliverable", "file")])
    job = runtime.jobs.submit(
        {
            "prompt": "do work",
            "worker_id": worker_id,
            "allowed_worker_ids": [worker_id],
            "collect_files": ["out.md"],
            "handoff": {"enabled": True, "worker_id": handoff_worker_id},
        }
    )
    job = _wait_terminal(runtime, job["id"])
    assert job["state"] == "succeeded"
    # wait for the handoff to be started (async).
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not runtime.db.get_job(job["id"]).get("handoff_job_id"):
        time.sleep(0.02)
    handoff_job_id = runtime.db.get_job(job["id"]).get("handoff_job_id")
    if handoff_job_id:
        # Let the handoff job's thread finish before teardown — otherwise it races the
        # TemporaryDirectory cleanup (writes into a deleted dir / reopens a deleted DB).
        _wait_terminal(runtime, handoff_job_id)
    events = _events(runtime, job["id"])
    seq = {event["event_type"]: event["seq"] for event in events}
    assert "files.collected" in seq, [event["event_type"] for event in events]
    assert "handoff_started" in seq, [event["event_type"] for event in events]
    # The barrier resolves BEFORE succeeded/handoff: collect precedes handoff (mutation: move
    # the barrier after the succeeded write -> handoff_started lands first -> red).
    assert seq["files.collected"] < seq["handoff_started"], seq
    print("  barrier ordering: collect precedes handoff OK")


def main() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        mock = ThreadingHTTPServer(("127.0.0.1", 0), MockWorker)
        threading.Thread(target=mock.serve_forever, daemon=True).start()
        base_url = f"http://127.0.0.1:{mock.server_address[1]}"

        runtime = AtlasRuntime(
            Config(
                host="127.0.0.1",
                port=0,
                db_path=root / "atlas.sqlite",
                api_token=None,
                request_timeout_seconds=2,
                enable_loopback_without_token=False,
                secret_key="file-collection-secret",
                upload_dir=root / "uploads",
            )
        )
        worker = runtime.db.upsert_worker({"name": "collector", "base_url": base_url})
        runtime.db.set_worker_sync_mode(worker["id"], "tunnel")
        handoff = runtime.db.upsert_worker({"name": "downstream", "base_url": base_url + "/x"})

        # A separate Atlas API server (distinct from the mock WORKER above) for the HTTP route test.
        api_server = AtlasHttpServer(("127.0.0.1", 0), runtime)
        threading.Thread(target=api_server.serve_forever, daemon=True).start()
        api_base = f"http://127.0.0.1:{api_server.server_address[1]}"
        user = runtime.db.create_user("admin", "admin-password", "admin")
        _, token = runtime.db.create_api_token(user["id"], "file-collection route check")

        try:
            check_happy_path(runtime, worker["id"])
            check_no_config_no_calls(runtime, worker["id"])
            check_disabled_skips(runtime, worker["id"])
            check_hostile_tar(runtime, worker["id"])
            check_symlink_rejected(runtime, worker["id"])
            check_caps_abort(runtime, worker["id"])
            check_409_then_success(runtime, worker["id"])
            check_persistent_409_gives_up(runtime, worker["id"])
            check_deadline(runtime, worker["id"])
            check_corrupt_tar(runtime, worker["id"])
            check_db_error_isolated(runtime, worker["id"])
            check_cancel_during_collection(runtime, worker["id"], handoff["id"])
            check_job_artifacts_route(runtime, api_base, worker["id"], token)
            check_barrier_ordering(runtime, worker["id"], handoff["id"])
        finally:
            api_server.shutdown()
            mock.shutdown()

    print("check_file_collection OK")


if __name__ == "__main__":
    main()
