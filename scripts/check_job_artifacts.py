#!/usr/bin/env python3
"""T9a hermetic Job Artifact contract check (stream, callback, validation, lease)."""
from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.config import Config
from atlas.app import AtlasRuntime

WORKSPACE_ID = ""


class MockArtifactsWorker(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []
    snapshots: dict[str, list[tuple[str, bytes]]] = {}
    manifest_override: Any = None
    body_override: tuple[bytes, str | None] | None = None
    body_overrides: dict[str, tuple[bytes, str | None]] = {}
    artifact_status: int | None = None
    block_manifest_for: str | None = None
    manifest_entered = threading.Event()
    manifest_release = threading.Event()

    def log_message(self, _format: str, *_args: object) -> None:
        return

    @classmethod
    def reset(cls) -> None:
        cls.requests.clear()
        cls.snapshots = {}
        cls.manifest_override = None
        cls.body_override = None
        cls.body_overrides = {}
        cls.artifact_status = None
        cls.block_manifest_for = None
        cls.manifest_entered.clear()
        cls.manifest_release.set()

    def _json(self, value: Any, status: int = 200) -> None:
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        self.__class__.requests.append({"path": self.path, "body": request})
        if self.path != "/agent/run":
            self._json({"error": "unexpected"}, 404)
            return
        session = request.get("session_id") or "sess-first"
        if session not in self.__class__.snapshots:
            self.__class__.snapshots[session] = [("out/report.txt", b"FROZEN")]
        callback = request.get("x_callback")
        if callback:
            self._json({"run_id": callback["run_id"], "session_id": session, "status": "accepted"}, 202)
            return
        frames = (
            f"event: session\ndata: {{\"id\":{json.dumps(session)}}}\n\n"
            "event: text\ndata: {\"delta\":\"done\"}\n\n"
            "data: [DONE]\n\n"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(frames)))
        self.end_headers()
        self.wfile.write(frames)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.path.startswith("/v1/sessions/"):
            self._json({"ok": True})
            return
        parts = parsed.path.split("/")
        session = parts[3]
        query = urllib.parse.parse_qs(parsed.query)
        self.__class__.requests.append({"path": self.path, "workspace_dir": query.get("workspace_dir", [None])[0]})
        if self.__class__.artifact_status is not None:
            self._json({"error": "artifact API unavailable"}, self.__class__.artifact_status)
            return
        if self.__class__.block_manifest_for == session and len(parts) == 5:
            self.__class__.manifest_entered.set()
            assert self.__class__.manifest_release.wait(5), "lease test did not release collection"
        rows = self.__class__.snapshots.get(session, [])
        if len(parts) == 5:
            if self.__class__.manifest_override is not None:
                self._json(self.__class__.manifest_override)
                return
            artifacts = [
                {"id": f"a{i + 1}", "path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                for i, (path, data) in enumerate(rows)
            ]
            # No `skipped` key on purpose: real thClaws omits it when empty (serde
            # skip_serializing_if), so the happy path must exercise the absent shape.
            self._json(
                {
                    "session_id": session,
                    "collected_at": "2099-01-01T00:00:00Z",
                    "patterns": ["out/*.txt"],
                    "artifacts": artifacts,
                }
            )
            return
        artifact_id = parts[5]
        index = int(artifact_id[1:]) - 1
        data = rows[index][1]
        sha = hashlib.sha256(data).hexdigest()
        if artifact_id in self.__class__.body_overrides:
            data, sha = self.__class__.body_overrides[artifact_id]
        elif self.__class__.body_override is not None:
            data, sha = self.__class__.body_override
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        if sha is not None:
            self.send_header("x-sha256", sha)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def wait_terminal(runtime: AtlasRuntime, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        job = runtime.db.get_job(job_id)
        if job and job["state"] in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not terminate")


def events(runtime: AtlasRuntime, job_id: str) -> list[dict[str, Any]]:
    return runtime.db.get_job_events_after(job_id, 0, limit=1000)


def submit(runtime: AtlasRuntime, worker_id: str, *, collect: list[str] | None = None, callback: bool = False, conversation_id: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "prompt": "work", "worker_id": worker_id, "workspace_id": WORKSPACE_ID, "allowed_worker_ids": [worker_id]
    }
    if collect is not None:
        payload["collect_files"] = collect
    if callback:
        payload["execution"] = "callback"
    if conversation_id:
        payload["conversation_id"] = conversation_id
    return runtime.jobs.submit(payload)


def stream_contract(runtime: AtlasRuntime, worker_id: str) -> None:
    MockArtifactsWorker.reset()
    MockArtifactsWorker.snapshots["sess-first"] = [("out/report.txt", b"FROZEN")]
    job = submit(runtime, worker_id, collect=["out/*.txt"])
    assert wait_terminal(runtime, job["id"])["state"] == "succeeded"
    run = next(row["body"] for row in MockArtifactsWorker.requests if row.get("path") == "/agent/run")
    assert run["collect_files"] == ["out/*.txt"], run
    assert not [row for row in MockArtifactsWorker.requests if row.get("path") == "/workspace/sync/export"]
    lookups = [row for row in MockArtifactsWorker.requests if str(row.get("path", "")).startswith("/v1/sessions/")]
    assert lookups and all(row["workspace_dir"] == "/tmp/t9-workspace" for row in lookups), lookups
    artifact = runtime.db.list_artifacts(job_id=job["id"], kind="file_ref")[0]
    assert (runtime.upload_dir / artifact["content"]).read_bytes() == b"FROZEN"
    assert artifact["metadata"]["sha256"] == hashlib.sha256(b"FROZEN").hexdigest()
    sequence = {event["event_type"]: event["seq"] for event in events(runtime, job["id"])}
    assert sequence["files.collected"] < sequence["state"], sequence
    print("  stream forwarding, workspace scope, frozen bytes, terminal barrier OK")


def no_collection_no_artifact_api(runtime: AtlasRuntime, worker_id: str) -> None:
    MockArtifactsWorker.reset()
    job = submit(runtime, worker_id)
    assert wait_terminal(runtime, job["id"])["state"] == "succeeded"
    assert not [row for row in MockArtifactsWorker.requests if str(row.get("path", "")).startswith("/v1/sessions/")]
    print("  no collect_files -> no Artifact API calls OK")


def malformed_and_integrity(runtime: AtlasRuntime, worker_id: str) -> None:
    cases: list[tuple[str, Any, tuple[bytes, str | None] | None]] = [
        ("duplicate", {"session_id": "sess-first", "artifacts": [
            {"id": "a1", "path": "ok.txt", "size": 1, "sha256": "0" * 64},
            {"id": "a1", "path": "other.txt", "size": 1, "sha256": "0" * 64},
        ]}, None),
        ("unsafe", {"session_id": "sess-first", "artifacts": [{"id": "a1", "path": "../bad", "size": 1, "sha256": "0" * 64}]}, None),
        ("skipped", {"session_id": "sess-first", "artifacts": [], "skipped": ["late.txt"]}, None),
        ("skipped-non-list", {"session_id": "sess-first", "collected_at": "2099-01-01T00:00:00Z", "patterns": ["out/*.txt"], "artifacts": [], "skipped": "late.txt"}, None),
        ("stale", {"session_id": "sess-first", "collected_at": "2000-01-01T00:00:00Z", "patterns": ["out/*.txt"], "artifacts": []}, None),
        ("bad-header", None, (b"FROZEN", "f" * 64)),
        ("bad-local-sha", None, (b"CHANGD", hashlib.sha256(b"FROZEN").hexdigest())),
        ("short-read", None, (b"SHORT", hashlib.sha256(b"SHORT").hexdigest())),
        ("oversized-read", None, (b"FROZEN-TOO-LONG", hashlib.sha256(b"FROZEN-TOO-LONG").hexdigest())),
    ]
    for label, manifest, body in cases:
        MockArtifactsWorker.reset()
        MockArtifactsWorker.snapshots["sess-first"] = [("out/report.txt", b"FROZEN")]
        MockArtifactsWorker.manifest_override = manifest
        MockArtifactsWorker.body_override = body
        before = set(runtime.upload_dir.iterdir())
        job = submit(runtime, worker_id, collect=["out/*.txt"])
        assert wait_terminal(runtime, job["id"])["state"] == "succeeded", label
        assert "files.collection_failed" in [event["event_type"] for event in events(runtime, job["id"])], label
        assert not runtime.db.list_artifacts(job_id=job["id"], kind="file_ref"), label
        assert set(runtime.upload_dir.iterdir()) == before, label
    MockArtifactsWorker.reset()
    MockArtifactsWorker.snapshots["sess-first"] = [("out/a.txt", b"A"), ("out/b.txt", b"B")]
    MockArtifactsWorker.body_overrides["a2"] = (b"X", hashlib.sha256(b"B").hexdigest())
    before = set(runtime.upload_dir.iterdir())
    job = submit(runtime, worker_id, collect=["out/*.txt"])
    assert wait_terminal(runtime, job["id"])["state"] == "succeeded"
    assert "files.collection_failed" in [event["event_type"] for event in events(runtime, job["id"])]
    assert not runtime.db.list_artifacts(job_id=job["id"], kind="file_ref")
    assert set(runtime.upload_dir.iterdir()) == before, "bad second member published the first"
    print("  malformed manifest, skipped, SHA/header/length mismatch -> no partial rows/blobs OK")


def caps_and_old_worker(runtime: AtlasRuntime, worker_id: str) -> None:
    original_files, original_bytes = runtime.jobs.artifact_max_files, runtime.jobs.artifact_max_bytes
    try:
        runtime.jobs.artifact_max_files = 1
        MockArtifactsWorker.reset()
        MockArtifactsWorker.snapshots["sess-first"] = [("out/a.txt", b"A"), ("out/b.txt", b"B")]
        job = submit(runtime, worker_id, collect=["out/*.txt"])
        assert wait_terminal(runtime, job["id"])["state"] == "succeeded"
        assert "files.collection_failed" in [event["event_type"] for event in events(runtime, job["id"])]
        assert not runtime.db.list_artifacts(job_id=job["id"], kind="file_ref")
    finally:
        runtime.jobs.artifact_max_files, runtime.jobs.artifact_max_bytes = original_files, original_bytes
    MockArtifactsWorker.reset()
    MockArtifactsWorker.artifact_status = 404
    job = submit(runtime, worker_id, collect=["out/*.txt"])
    assert wait_terminal(runtime, job["id"])["state"] == "succeeded"
    assert "files.collection_failed" in [event["event_type"] for event in events(runtime, job["id"])]
    assert not [row for row in MockArtifactsWorker.requests if row.get("path") == "/workspace/sync/export"]
    print("  manifest caps and old-worker failure have no partial rows/blobs or sync fallback OK")


def callback_contract(runtime: AtlasRuntime, worker_id: str) -> None:
    MockArtifactsWorker.reset()
    MockArtifactsWorker.snapshots["sess-first"] = [("out/callback.txt", b"CALLBACK-FROZEN")]
    job = submit(runtime, worker_id, collect=["out/*.txt"], callback=True)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not any(row["body"].get("x_callback") for row in MockArtifactsWorker.requests if row.get("path") == "/agent/run"):
        time.sleep(0.02)
    run = next(row["body"] for row in MockArtifactsWorker.requests if row.get("path") == "/agent/run")
    assert run["collect_files"] == ["out/*.txt"], run
    result = runtime.jobs.apply_worker_callback(job["id"], {"run_id": job["id"], "status": "succeeded", "summary": "ok"})
    assert result == {"applied": True, "state": "succeeded"}, result
    duplicate = runtime.jobs.apply_worker_callback(job["id"], {"run_id": job["id"], "status": "succeeded"})
    assert duplicate == {"applied": False, "state": "succeeded"}, duplicate
    rows = runtime.db.list_artifacts(job_id=job["id"], kind="file_ref")
    assert len(rows) == 1 and (runtime.upload_dir / rows[0]["content"]).read_bytes() == b"CALLBACK-FROZEN", rows
    sequence = {event["event_type"]: event["seq"] for event in events(runtime, job["id"])}
    assert sequence["files.collected"] < sequence["state"], sequence
    print("  callback forwarding/collection/idempotent terminal barrier OK")


def session_lease(runtime: AtlasRuntime, worker_id: str) -> None:
    MockArtifactsWorker.reset()
    MockArtifactsWorker.snapshots["sess-first"] = [("out/first.txt", b"FIRST-FROZEN")]
    MockArtifactsWorker.block_manifest_for = "sess-first"
    MockArtifactsWorker.manifest_release.clear()
    first = submit(runtime, worker_id, collect=["out/*.txt"])
    assert MockArtifactsWorker.manifest_entered.wait(5), "first job did not enter collection"
    first_row = runtime.db.get_job(first["id"])
    second = runtime.jobs.submit({"prompt": "continued", "conversation_id": first_row["conversation_id"]})
    time.sleep(0.2)
    agent_runs = [row["body"] for row in MockArtifactsWorker.requests if row.get("path") == "/agent/run"]
    assert len(agent_runs) == 1, "continued job dispatched before first collection released its lease"
    MockArtifactsWorker.manifest_release.set()
    assert wait_terminal(runtime, first["id"])["state"] == "succeeded"
    assert wait_terminal(runtime, second["id"])["state"] == "succeeded"
    rows = runtime.db.list_artifacts(job_id=first["id"], kind="file_ref")
    assert len(rows) == 1 and (runtime.upload_dir / rows[0]["content"]).read_bytes() == b"FIRST-FROZEN"
    print("  continued-session lease blocks interleaving until frozen collection terminalizes OK")


def lease_loser_cleanup(runtime: AtlasRuntime, worker_id: str) -> None:
    """Reaper wins the terminal race mid-collection: the terminal owner's lease must SURVIVE
    the claim backstop while its collector's inflight flag is up (a waiter must not mutate the
    session snapshot mid-download), and the LOSING collector must then clear the flag and
    release the lease itself — that handshake is what un-wedges the session's waiters."""
    MockArtifactsWorker.reset()
    MockArtifactsWorker.snapshots["sess-first"] = [("out/late.txt", b"LATE-FROZEN")]
    before = set(runtime.upload_dir.iterdir())
    job = submit(runtime, worker_id, collect=["out/*.txt"], callback=True)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not (runtime.db.get_job(job["id"]) or {}).get("thclaws_session_id"):
        time.sleep(0.02)  # _record_session claims the lease BEFORE persisting the session id
    MockArtifactsWorker.block_manifest_for = "sess-first"
    MockArtifactsWorker.manifest_entered.clear()
    MockArtifactsWorker.manifest_release.clear()
    outcome: dict[str, Any] = {}

    def _deliver() -> None:
        outcome["result"] = runtime.jobs.apply_worker_callback(job["id"], {"run_id": job["id"], "status": "succeeded"})

    delivery = threading.Thread(target=_deliver)
    delivery.start()
    try:
        assert MockArtifactsWorker.manifest_entered.wait(5), "callback delivery did not enter collection"
        # Force the reaper to terminal-ize the job while the collector is blocked mid-download.
        runtime.db.update_job(job["id"], callback_deadline_at="2000-01-01T00:00:00Z")
        original_grace = runtime.jobs.callback_reap_grace_seconds
        runtime.jobs.callback_reap_grace_seconds = 0.0
        try:
            runtime.jobs.reap_callback_jobs()
        finally:
            runtime.jobs.callback_reap_grace_seconds = original_grace
        row = runtime.db.get_job(job["id"]) or {}
        assert row.get("state") == "failed" and row.get("collection_inflight") == 1, row
        # A continuation submitted now must NOT dispatch: the terminal owner's lease survives
        # the claim backstop while its collector's inflight flag is up.
        first_row = runtime.db.get_job(job["id"]) or {}
        waiter = runtime.jobs.submit({"prompt": "continued", "conversation_id": first_row["conversation_id"]})
        time.sleep(0.2)
        agent_runs = [r for r in MockArtifactsWorker.requests if r.get("path") == "/agent/run"]
        assert len(agent_runs) == 1, "waiter dispatched while the losing collector was still inflight"
    finally:
        MockArtifactsWorker.manifest_release.set()
    delivery.join(5)
    assert not delivery.is_alive(), "callback delivery thread wedged"
    assert outcome.get("result") == {"applied": False, "state": "failed"}, outcome
    row = runtime.db.get_job(job["id"]) or {}
    assert row.get("collection_inflight") == 0, row
    assert not runtime.db.list_artifacts(job_id=job["id"], kind="file_ref")
    assert set(runtime.upload_dir.iterdir()) == before, "losing collector leaked staged blobs"
    # The loser cleared its flag and released the lease — the waiter must now proceed.
    assert wait_terminal(runtime, waiter["id"])["state"] == "succeeded"
    print("  reaper-vs-collector: guard holds mid-download, loser clears inflight + lease OK")


def main() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        server = ThreadingHTTPServer(("127.0.0.1", 0), MockArtifactsWorker)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        runtime = AtlasRuntime(
            Config(
                host="127.0.0.1", port=0, db_path=root / "atlas.sqlite", api_token=None,
                request_timeout_seconds=2, enable_loopback_without_token=True,
                secret_key="t9a-check", public_base_url="http://atlas.invalid", upload_dir=root / "uploads",
            )
        )
        worker = runtime.db.upsert_worker({"name": "t9a", "base_url": f"http://127.0.0.1:{server.server_address[1]}"})
        workspace = runtime.db.upsert_workspace({"worker_id": worker["id"], "workspace_key": "t9", "workspace_dir": "/tmp/t9-workspace"})
        global WORKSPACE_ID
        WORKSPACE_ID = workspace["id"]
        try:
            worker_id = worker["id"]
            stream_contract(runtime, worker_id)
            no_collection_no_artifact_api(runtime, worker_id)
            malformed_and_integrity(runtime, worker_id)
            caps_and_old_worker(runtime, worker_id)
            callback_contract(runtime, worker_id)
            session_lease(runtime, worker_id)
            lease_loser_cleanup(runtime, worker_id)
        finally:
            runtime.close()  # stop the reaper daemon before the tempdir exits
            server.shutdown()
            server.server_close()
    print("check_job_artifacts OK")


if __name__ == "__main__":
    main()
