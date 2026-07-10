"""T6 — file handoff (push to the next worker). Hermetic checks with TWO mock thClaws workers:
a collector (A) whose Job Artifact snapshot supplies files, and a consumer (B) whose /workspace/sync/push
captures the pushed tar. Covers the end-to-end push, the {files_dir} prompt substitution, the
additive `incoming/<run_id>/<node_key>/` layout, the policy.file_handoff opt-in (save-time
validator AND runtime guard), push-failure fails the edge, and trash/replace never called.

Mutation targets (break the code -> this file goes red):
- drop the runtime `policy.file_handoff` guard in the _execute_run node loop (do_push =
  bool(pushes)) -> a push happens with no policy -> check_runtime_guard sees B receive a tar.
- remove the push_files/file_handoff save-time check in validate_workflow_graph
  -> check_validation_no_policy stops raising.
- drop the upload-store containment check in _push_files_to_worker
  -> check_hostile_artifact_rejected reads an out-of-store file and pushes it.
"""

from __future__ import annotations

import io
import json
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

import atlas.workflows as workflows_module
from atlas.app import AtlasRuntime
from atlas.config import Config
from atlas.workflows import validate_workflow_graph

A_FILE = b"deliverable-produced-by-A\n"


def _gzip_tar(members: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class _Base(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json(self, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream_ok(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b'event: text\ndata: {"text": "ok"}\n\n')
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._json({"ok": True})
        elif self.path == "/v1/agent/info":
            self._json({"version": "0.85.0"})
        elif self.path == "/v1/models":
            self._json({"object": "list", "data": []})
        else:
            self.send_error(HTTPStatus.NOT_FOUND)


class WorkerA(_Base):
    """Collector: its frozen Job Artifact supplies A_FILE as out.md."""

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        _ = self.rfile.read(length) if length else b""
        if self.path == "/agent/run":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'event: session\ndata: {"id":"sess-a"}\n\n')
            self.wfile.write(b'event: text\ndata: {"text": "ok"}\n\n')
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_GET(self) -> None:
        import hashlib

        if self.path.startswith("/v1/sessions/sess-a/artifacts/"):
            body = A_FILE
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("x-sha256", hashlib.sha256(body).hexdigest())
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/v1/sessions/sess-a/artifacts"):
            # No `skipped` key: real thClaws omits it when empty (serde skip_serializing_if).
            self._json({"session_id": "sess-a", "collected_at": "2099-01-01T00:00:00Z", "patterns": ["out.md"], "artifacts": [{"id": "a1", "path": "out.md", "size": len(A_FILE), "sha256": hashlib.sha256(A_FILE).hexdigest()}]})
            return
        super().do_GET()


class WorkerB(_Base):
    """Consumer: captures pushed tars; records forbidden trash/replace calls; push can be forced
    to fail."""

    pushed_tars: list = []
    push_calls = 0
    trash_calls = 0
    push_status = 200
    push_delay = 0.0

    def do_POST(self) -> None:
        cls = type(self)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if self.path == "/agent/run":
            self._stream_ok()
        elif self.path == "/workspace/sync/push":
            cls.push_calls += 1
            if cls.push_delay:
                time.sleep(cls.push_delay)
            if cls.push_status != 200:
                self.send_error(cls.push_status)
                return
            cls.pushed_tars.append(body)
            self._json({"ok": True})
        elif self.path in ("/workspace/sync/trash", "/workspace/sync/replace"):
            cls.trash_calls += 1  # MUST stay 0 — Atlas never clobbers the target
            self._json({"ok": True})
        else:
            self.send_error(HTTPStatus.NOT_FOUND)


def _reset_b() -> None:
    WorkerB.pushed_tars = []
    WorkerB.push_calls = 0
    WorkerB.trash_calls = 0
    WorkerB.push_status = 200
    WorkerB.push_delay = 0.0


def _members(tar_bytes: bytes) -> dict[str, bytes]:
    out = {}
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        for member in tar.getmembers():
            out[member.name] = tar.extractfile(member).read()
    return out


def _graph(a_id: str, b_id: str, push_files=("files.collector.*",)) -> dict:
    return {
        "start": "collector",
        "nodes": [
            {"id": "collector", "type": "worker", "worker_id": a_id, "prompt": "produce", "collect_files": ["out.md"]},
            {"id": "consumer", "type": "worker", "worker_id": b_id, "prompt": "consume files at {files_dir}"},
        ],
        "edges": [{"from": "collector", "to": "consumer", "push_files": list(push_files)}],
    }


def check_end_to_end(runtime: AtlasRuntime, a_id: str, b_id: str) -> None:
    _reset_b()
    policy = {"file_handoff": True, "allowed_worker_ids": [a_id, b_id], "max_jobs": 10}
    run = runtime.workflows.run_graph(_graph(a_id, b_id), policy)
    assert run["state"] == "succeeded", run.get("error")

    # B received exactly one push, byte-identical to A's out.md, under incoming/<run>/consumer/.
    assert WorkerB.push_calls == 1, WorkerB.push_calls
    assert len(WorkerB.pushed_tars) == 1
    members = _members(WorkerB.pushed_tars[0])
    arcname = f"incoming/{run['id']}/consumer/out.md"
    assert arcname in members, list(members)
    assert members[arcname] == A_FILE, "pushed bytes must be byte-identical to A's file"

    # trash/replace never called (additive-only).
    assert WorkerB.trash_calls == 0, "Atlas must never call trash/replace"

    # the consumer job's prompt carries {files_dir} substituted to the incoming prefix.
    consumer_node = next(n for n in runtime.db.list_workflow_nodes(run["id"]) if n["node_key"] == "consumer")
    consumer_job = runtime.db.get_job(consumer_node["job_id"])
    assert f"incoming/{run['id']}/consumer" in consumer_job["prompt"], consumer_job["prompt"]

    # files.pushed audited before the downstream job (count/bytes/target).
    pushed = [row for row in runtime.db.list_audit() if row["action"] == "files.pushed"]
    assert pushed and pushed[0]["details"]["target_worker_id"] == b_id and pushed[0]["details"]["count"] == 1
    print("  end-to-end: A -> push -> B byte-identical; {files_dir} substituted; trash never called OK")


def check_validation_no_policy(a_id: str, b_id: str) -> None:
    # push_files WITHOUT policy.file_handoff -> save-time validation error (mutation: remove the
    # cross-check -> no raise -> red).
    try:
        validate_workflow_graph(_graph(a_id, b_id), {"allowed_worker_ids": [a_id, b_id]})
        raise AssertionError("push_files without policy.file_handoff must be rejected")
    except ValueError as exc:
        assert "file_handoff" in str(exc), exc
    # with the opt-in it validates.
    validate_workflow_graph(_graph(a_id, b_id), {"file_handoff": True, "allowed_worker_ids": [a_id, b_id]})
    # a non-boolean file_handoff is rejected by the policy validator.
    from atlas.workflows import validate_workflow_policy

    try:
        validate_workflow_policy({"file_handoff": "yes"})
        raise AssertionError("non-boolean file_handoff must be rejected")
    except ValueError:
        pass
    # T9a supports collection on callback jobs; graph validation must preserve that
    # combination rather than retaining T5's old rejection.
    graph = _graph(a_id, b_id)
    graph["nodes"][0]["execution"] = "callback"
    validate_workflow_graph(graph, {"file_handoff": True, "allowed_worker_ids": [a_id, b_id]})
    print("  save-time validation: push_files requires policy.file_handoff OK")


def check_runtime_guard(runtime: AtlasRuntime, a_id: str, b_id: str) -> None:
    # Bypass the save-time validator and run a push_files graph with policy.file_handoff OFF.
    # The runtime guard in the _execute_run node loop must prevent any push (mutation: drop it
    # -> B receives a tar -> red).
    _reset_b()
    original = workflows_module.validate_workflow_graph
    workflows_module.validate_workflow_graph = lambda graph, policy=None: graph  # type: ignore[assignment]
    try:
        policy = {"allowed_worker_ids": [a_id, b_id], "max_jobs": 10}  # NO file_handoff
        run = runtime.workflows.run_graph(_graph(a_id, b_id), policy)
    finally:
        workflows_module.validate_workflow_graph = original  # type: ignore[assignment]
    assert run["state"] == "succeeded", run.get("error")
    assert WorkerB.push_calls == 0, "no push may happen without policy.file_handoff"
    print("  runtime guard: no push without policy.file_handoff OK")


def check_push_failure_fails_edge(runtime: AtlasRuntime, a_id: str, b_id: str) -> None:
    _reset_b()
    WorkerB.push_status = HTTPStatus.INTERNAL_SERVER_ERROR
    policy = {"file_handoff": True, "allowed_worker_ids": [a_id, b_id], "max_jobs": 10}
    run = runtime.workflows.run_graph(_graph(a_id, b_id), policy)
    # push failed -> consumer node fails -> run fails loudly (stop_on_first_failure defaults True).
    assert run["state"] == "failed", run["state"]
    node_states = {n["node_key"]: n["state"] for n in runtime.db.list_workflow_nodes(run["id"])}
    assert node_states.get("consumer") == "failed", node_states
    # continue-on-failure: with stop_on_first_failure False the run continues and records it.
    _reset_b()
    WorkerB.push_status = HTTPStatus.INTERNAL_SERVER_ERROR
    run2 = runtime.workflows.run_graph(_graph(a_id, b_id), {**policy, "stop_on_first_failure": False})
    failures = [row for row in runtime.db.list_workflow_events(run2["id"]) if row["event_type"] == "failure_recorded"]
    assert failures, "continue-on-failure must audit the skipped edge"
    print("  push failure -> edge fails loudly; continue-on-failure audited OK")


def check_hostile_artifact_rejected(runtime: AtlasRuntime, a_id: str, b_id: str) -> None:
    # A file_ref artifact is not necessarily from T5's validated collection — POST /api/artifacts
    # lets a caller set an arbitrary content path + relpath. The push MUST re-validate both, or it
    # would exfiltrate an arbitrary host file / escape the incoming/ prefix. Mutation: drop the
    # containment check in _push_files_to_worker -> case A reads the out-of-store file and pushes.
    from atlas.sync_files import store_bytes

    policy = {"file_handoff": True, "allowed_worker_ids": [a_id, b_id], "max_jobs": 10}
    run = runtime.workflows.run_graph(_graph(a_id, b_id), policy)
    _reset_b()  # clear the legit push from the run above
    run_row = runtime.db.get_workflow_run(run["id"])
    node = {"id": "consumer"}

    # A sentinel OUTSIDE the upload store; a traversal content must not reach it.
    secret = runtime.jobs.upload_dir.parent / "secret.txt"
    secret.write_bytes(b"TOP-SECRET-HOST-FILE")

    # Case A: content escapes the upload store -> rejected, nothing pushed.
    runtime.db.create_artifact(
        {"run_id": run["id"], "key": "files.evil.a", "kind": "file_ref", "content": "../secret.txt", "metadata": {"relpath": "a"}}
    )
    try:
        runtime.workflows._push_files_to_worker(run_row, node, b_id, [{"from": "evil", "push_files": ["files.evil.a"]}], "incoming/x")
        raise AssertionError("out-of-store content path must be rejected")
    except ValueError:
        pass
    assert WorkerB.push_calls == 0, "no bytes may be pushed for a rejected artifact"

    # Case B: valid in-store content but a traversal relpath -> rejected (arcname escape).
    opaque, _ = store_bytes(runtime.jobs.upload_dir, b"legit")
    runtime.db.create_artifact(
        {"run_id": run["id"], "key": "files.evil.b", "kind": "file_ref", "content": opaque, "metadata": {"relpath": "../escape"}}
    )
    try:
        runtime.workflows._push_files_to_worker(run_row, node, b_id, [{"from": "evil", "push_files": ["files.evil.b"]}], "incoming/x")
        raise AssertionError("traversal relpath must be rejected")
    except ValueError:
        pass
    assert WorkerB.push_calls == 0
    print("  hostile artifact (out-of-store content / traversal relpath) rejected OK")


def check_budget_gate_precedes_push(runtime: AtlasRuntime, a_id: str, b_id: str) -> None:
    # An unaffordable node must be rejected BEFORE its files are pushed into the target
    # worker's workspace — the push is a side effect on another machine, not something
    # _wait_for_job can undo. max_budget_units=1 lets the collector run and exhausts the
    # budget before the consumer. (Mutation: drop the _check_budget pre-check in front of
    # _push_files_to_worker → WorkerB receives the tar before the node fails → red.)
    _reset_b()
    policy = {"file_handoff": True, "allowed_worker_ids": [a_id, b_id], "max_jobs": 10, "max_budget_units": 1}
    run = runtime.workflows.run_graph(_graph(a_id, b_id), policy)
    assert run["state"] == "failed", run["state"]
    assert "budget" in (run.get("error") or ""), run.get("error")
    assert WorkerB.push_calls == 0, "files were pushed to the worker before the budget rejection"
    print("  budget gate precedes the push side-effect OK")


def check_batch_create_is_all_or_nothing(runtime: AtlasRuntime) -> None:
    # create_artifacts validates EVERY payload before any row is written — an invalid entry
    # anywhere in the list publishes nothing. (Mutation: validate/insert row-by-row → the
    # first row survives → red.)
    bad_batch = [
        {"key": "batch.ok", "kind": "text", "content": "fine"},
        {"key": "", "kind": "text", "content": "invalid: empty key"},
    ]
    try:
        runtime.db.create_artifacts(bad_batch)
        raise AssertionError("invalid payload in a batch must raise")
    except ValueError:
        pass
    leaked = [art for art in runtime.db.iter_artifacts() if art["key"] == "batch.ok"]
    assert not leaked, "a batch with an invalid entry published its earlier rows"
    print("  create_artifacts is all-or-nothing OK")


def check_cancel_during_push_no_downstream_job(runtime: AtlasRuntime, a_id: str, b_id: str) -> None:
    # A cancel landing while the UNLOCKED push blocks must prevent the downstream job from
    # ever being created — not create-then-reap. (Mutation: drop the cancelled re-check under
    # the lock before _reserve_and_submit_job → the consumer job appears → red.)
    _reset_b()
    WorkerB.push_delay = 1.0
    policy = {"file_handoff": True, "allowed_worker_ids": [a_id, b_id], "max_jobs": 10}
    result: dict = {}

    def run_it() -> None:
        try:
            result["run"] = runtime.workflows.run_graph(_graph(a_id, b_id), policy)
        except Exception as exc:  # surfaced below; the thread must never die silently
            result["error"] = exc

    thread = threading.Thread(target=run_it)
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and WorkerB.push_calls == 0:
        time.sleep(0.02)
    assert WorkerB.push_calls == 1, "push never started"
    running = [row for row in runtime.db.list_workflow_runs(limit=10) if row["state"] == "running"]
    assert running, "no running run to cancel"
    run_id = running[0]["id"]
    runtime.workflows.cancel_run(run_id)  # lands inside WorkerB's 1.0s push delay
    thread.join(timeout=30)
    assert not thread.is_alive(), "runner thread wedged after cancel-during-push"
    run_row = runtime.db.get_workflow_run(run_id)
    assert run_row and run_row["state"] == "cancelled", run_row and run_row["state"]
    consumer = [n for n in runtime.db.list_workflow_nodes(run_id) if n["node_key"] == "consumer"]
    assert not consumer or not consumer[0].get("job_id"), (
        f"downstream job {consumer[0].get('job_id')} was created for a cancelled run"
    )
    # The cancelled node must land 'cancelled', not 'failed' — the submit guard raises
    # _WorkflowCancelled, not a generic error. (Mutation: raise ValueError instead → the node
    # is written 'failed' and a node_failed event is emitted → red.)
    assert consumer and consumer[0]["state"] == "cancelled", (
        f"cancelled node landed as {consumer[0]['state'] if consumer else 'missing'}, expected 'cancelled'"
    )
    events = runtime.db.list_workflow_events(run_id)
    node_failed = [e for e in events if e["event_type"] == "node_failed" and e.get("node_key") == "consumer"]
    assert not node_failed, "a cancelled node must not emit node_failed"
    print("  cancel during push: no downstream job, node cancelled (not failed) OK")


def check_deadline_precedes_submit(runtime: AtlasRuntime, a_id: str, b_id: str) -> None:
    # A run whose max_minutes deadline expires DURING the unlocked push must not then create
    # and submit the downstream job — the deadline is re-checked under the lock before
    # _reserve_and_submit_job. max_minutes is validated as a whole-minute integer (untestable
    # in a gate), so we stub _check_deadline to trip only AFTER the push side-effect has
    # happened — deterministic, no wall-clock race. (Mutation: drop the under-lock
    # _check_deadline(deadline) → the consumer job is created and submitted even though the
    # deadline expired mid-push → consumer node gets a job_id → red.)
    _reset_b()
    policy = {"file_handoff": True, "allowed_worker_ids": [a_id, b_id], "max_jobs": 10, "max_minutes": 1}
    original = workflows_module._check_deadline

    def trip_after_push(_deadline: object) -> None:
        # Let every check pass until the push has run; the next check (the under-lock one, just
        # before submit) is the one that must fire.
        if WorkerB.push_calls >= 1:
            raise workflows_module._WorkflowGuardTripped("workflow policy max_minutes exceeded")

    workflows_module._check_deadline = trip_after_push
    try:
        run = runtime.workflows.run_graph(_graph(a_id, b_id), policy)
    finally:
        workflows_module._check_deadline = original
    assert run["state"] == "failed", run["state"]
    assert "max_minutes" in (run.get("error") or ""), run.get("error")
    assert WorkerB.push_calls == 1, "the push (side effect) should have happened before the deadline trip"
    consumer = [n for n in runtime.db.list_workflow_nodes(run["id"]) if n["node_key"] == "consumer"]
    assert consumer and not consumer[0].get("job_id"), (
        f"a downstream job was created after the deadline expired: {consumer and consumer[0].get('job_id')}"
    )
    print("  deadline expiring during push blocks the downstream submit OK")


def check_artifact_iteration_unbounded(runtime: AtlasRuntime) -> None:
    # The push glob resolution and the artifact routes iterate ALL artifacts via rowid keyset
    # paging — no fixed window. Prove the iterator crosses its batch boundary where the old
    # windowed read truncates. (Mutation: drop iter_artifacts' keyset loop → ≤ one batch of
    # 500 comes back → red.)
    total = 1200  # > 2 batches of 500; cheap: one transaction, text artifacts, no fsync
    # run_id/job_id stay NULL (both are FK-enforced); the probe keys identify our rows.
    runtime.db.create_artifacts(
        [{"key": f"probe.{i}", "kind": "text", "content": "x"} for i in range(total)]
    )
    seen = [art["key"] for art in runtime.db.iter_artifacts() if art["key"].startswith("probe.")]
    assert len(seen) == total, f"iterator truncated: {len(seen)} of {total}"
    assert seen[0] == "probe.0" and seen[-1] == f"probe.{total - 1}", "keyset paging broke ordering"
    assert len(runtime.db.list_artifacts(limit=1000)) == 1000, "windowed read behaviour changed (limit no longer applies)"
    print("  iter_artifacts pages past any fixed window OK")


def main() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        server_a = ThreadingHTTPServer(("127.0.0.1", 0), WorkerA)
        server_b = ThreadingHTTPServer(("127.0.0.1", 0), WorkerB)
        threading.Thread(target=server_a.serve_forever, daemon=True).start()
        threading.Thread(target=server_b.serve_forever, daemon=True).start()
        url_a = f"http://127.0.0.1:{server_a.server_address[1]}"
        url_b = f"http://127.0.0.1:{server_b.server_address[1]}"

        runtime = AtlasRuntime(
            Config(
                host="127.0.0.1",
                port=0,
                db_path=root / "atlas.sqlite",
                api_token=None,
                request_timeout_seconds=3,
                enable_loopback_without_token=False,
                secret_key="file-handoff-secret",
                upload_dir=root / "uploads",
            )
        )
        worker_a = runtime.db.upsert_worker({"name": "collector-A", "base_url": url_a, "tags": ["collect"]})
        worker_b = runtime.db.upsert_worker({"name": "consumer-B", "base_url": url_b, "tags": ["consume"]})
        runtime.db.set_worker_sync_mode(worker_a["id"], "tunnel")
        runtime.db.set_worker_sync_mode(worker_b["id"], "tunnel")

        try:
            check_end_to_end(runtime, worker_a["id"], worker_b["id"])
            check_validation_no_policy(worker_a["id"], worker_b["id"])
            check_runtime_guard(runtime, worker_a["id"], worker_b["id"])
            check_push_failure_fails_edge(runtime, worker_a["id"], worker_b["id"])
            check_hostile_artifact_rejected(runtime, worker_a["id"], worker_b["id"])
            check_budget_gate_precedes_push(runtime, worker_a["id"], worker_b["id"])
            check_batch_create_is_all_or_nothing(runtime)
            check_cancel_during_push_no_downstream_job(runtime, worker_a["id"], worker_b["id"])
            check_deadline_precedes_submit(runtime, worker_a["id"], worker_b["id"])
            check_artifact_iteration_unbounded(runtime)
        finally:
            runtime.close()  # stop the reaper daemon before the tempdir exits
            server_a.shutdown()
            server_b.shutdown()

    print("check_file_handoff OK")


if __name__ == "__main__":
    main()
