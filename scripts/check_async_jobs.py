"""T3 hermetic checks: async execution via x_callback.

Covers the plan's mandatory list (docs/plans/thclaws-api-adoption-plan.md, Milestone T3):
pre-auth callback route reachable with ONLY the HMAC api_key; oversized body rejected before
processing; end-to-end apply (terminal state, usage, system audit actor) with duplicate
delivery converging; bad/expired/cross-job token 401 with the job unaffected; token validity
covering deadline + the worker's retry envelope (simulated clock); reaper at the deadline (and
not before); callback-vs-reaper single terminal state; restart preserving callback-pending
jobs; submit/graph validation. Own temp DB, ephemeral ports, mock thClaws worker.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.app import AtlasHttpServer, AtlasRuntime, _CALLBACK_MAX_BODY_BYTES
from atlas.config import Config
from atlas.db import Database
from atlas.jobs import (
    CALLBACK_RETRY_ENVELOPE_SECONDS,
    JobManager,
    _handoff_child_id,
    mint_callback_token,
    verify_callback_token,
)
from atlas.workflows import (
    _CALLBACK_WAIT_SWEEP_MARGIN_SECONDS,
    _callback_wait_extra_seconds,
    WorkflowRunner,
    validate_workflow_graph,
)

SECRET = "cb-check-secret"
# x_callback envelopes captured by the mock worker, keyed by run_id (= Atlas job id).
CAPTURED: dict[str, dict[str, Any]] = {}


class MockAsyncWorker(BaseHTTPRequestHandler):
    """Mock thClaws in x_callback mode: 202-ACKs /agent/run and captures the envelope so the
    check can play the worker's delivery role deterministically."""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        body = json.dumps({"ok": True} if self.path == "/healthz" else {"name": "mock-async"}).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        callback = request.get("x_callback")
        assert isinstance(callback, dict), f"x_callback must be the OBJECT envelope, got: {callback!r}"
        assert callback.get("url") and callback.get("api_key") and callback.get("run_id"), callback
        CAPTURED[callback["run_id"]] = callback
        ack = {
            "run_id": callback["run_id"],
            "session_id": "sess-async-1",
            "status": "accepted",
            "model": request.get("model") or "",
        }
        body = json.dumps(ack).encode("utf-8")
        self.send_response(HTTPStatus.ACCEPTED)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start_mock_worker() -> tuple[ThreadingHTTPServer, str]:
    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockAsyncWorker)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    host, port = mock.server_address
    return mock, f"http://{host}:{port}"


def _make_runtime(tmp: Path, name: str) -> AtlasRuntime:
    return AtlasRuntime(
        Config(
            host="127.0.0.1",
            port=0,
            db_path=tmp / f"{name}.sqlite",
            api_token=None,
            request_timeout_seconds=5,
            enable_loopback_without_token=False,  # loopback bypass OFF: only the HMAC token can authorize
            secret_key=SECRET,
            upload_dir=tmp / f"{name}-uploads",
            public_base_url="http://atlas.invalid",  # replaced with the live address once the server binds
            callback_timeout_seconds=1800,  # non-default on purpose: asserts Config reaches JobManager
        )
    )


def _start_atlas(runtime: AtlasRuntime) -> tuple[AtlasHttpServer, str]:
    server = AtlasHttpServer(("127.0.0.1", 0), runtime)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    # Now the worker-deliverable base URL is known; the captured envelope url becomes real.
    runtime.jobs.public_base_url = base
    return server, base


def _wait(predicate, timeout: float = 5.0, message: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {message}")


def _post_callback(url: str, token: str | None, payload: Any, raw_body: bytes | None = None) -> tuple[int, dict[str, Any]]:
    body = raw_body if raw_body is not None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # nosec B310 - loopback test server
            return response.getcode(), json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(detail or "{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": detail}


def _terminal_payload(job_id: str, status: str = "succeeded", summary: str = "final answer") -> dict[str, Any]:
    """The real thClaws CallbackPayload wire shape (crates/core/src/api_v1/callback.rs)."""
    return {
        "run_id": job_id,
        "status": status,
        "finish_reason": "stop" if status == "succeeded" else "error",
        "model": "claude-haiku-4-5",
        "summary": summary,
        "usage": {
            "prompt_tokens": 111,
            "completion_tokens": 22,
            "total_tokens": 133,
            "cached_input_tokens": 7,
        },
        "tool_calls": ["Read", "Bash"],
        "tool_denials": [],
        "iterations": 3,
        "started_at": "2026-07-04T00:00:00Z",
        "completed_at": "2026-07-04T00:01:00Z",
    }


def _submit_callback_job(runtime: AtlasRuntime, worker_base: str, prompt: str = "hi") -> dict[str, Any]:
    worker = runtime.db.upsert_worker({"base_url": worker_base, "name": f"mock-{prompt[:12]}"})
    job = runtime.jobs.submit({"prompt": prompt, "worker_id": worker["id"], "execution": "callback"})
    # Wait for the dispatch to be FULLY processed, not merely captured. The mock writes CAPTURED
    # BEFORE it sends the 202 ACK, so `job_id in CAPTURED` can win while Atlas is still reading
    # the ACK and persisting thclaws_session_id / callback_deadline_at — a caller reading those
    # right after would flake on None. `callback_dispatched` is the LAST write of the dispatch
    # path (after the session binding), so it is the settled marker; CAPTURED is already set by
    # the time it fires, so callers that read the envelope stay safe.
    _wait(
        lambda: any(
            event["event_type"] == "callback_dispatched"
            for event in runtime.db.get_job_events_after(job["id"], 0, limit=1000)
        ),
        message="x_callback dispatch",
    )
    return runtime.db.get_job(job["id"]) or job


def check_callback_end_to_end(tmp: Path) -> None:
    """Dispatch → 202 ACK (session bound) → terminal callback with ONLY the HMAC api_key (no
    user token, loopback bypass off) → job terminal, usage recorded, system audit actor;
    duplicate delivery converges to one terminal state with nothing double-applied."""
    mock, worker_base = _start_mock_worker()
    runtime = _make_runtime(tmp, "e2e")
    server, base = _start_atlas(runtime)
    try:
        # Config.callback_timeout_seconds must actually reach the JobManager (not just the env).
        assert runtime.jobs.callback_timeout_seconds == 1800, runtime.jobs.callback_timeout_seconds

        job = _submit_callback_job(runtime, worker_base, "async e2e")
        job_id = job["id"]

        # Callback-pending shape: running, deadline set, ACK session bound, mode persisted.
        assert job["state"] == "running", job["state"]
        assert job.get("execution") == "callback", job
        assert job.get("callback_deadline_at"), job
        assert job.get("thclaws_session_id") == "sess-async-1", job

        envelope = CAPTURED[job_id]
        assert envelope["url"] == f"{base}/api/worker-callbacks/{job_id}", envelope
        assert envelope["run_id"] == job_id, envelope
        token = envelope["api_key"]

        # The signed token must never be stored: byte-scan the DB (and WAL) for it.
        for suffix in ("", "-wal"):
            path = Path(str(runtime.db.path) + suffix)
            if path.exists():
                assert token.encode("utf-8") not in path.read_bytes(), f"callback token leaked into {path.name}"

        # Deliver with ONLY the Bearer api_key — no user token exists on this request, and the
        # loopback bypass is off, so this succeeding proves the pre-auth carve-out (routing the
        # path through _is_authorized() turns this into a 401 and the check red).
        status, result = _post_callback(envelope["url"], token, _terminal_payload(job_id))
        assert status == 200 and result == {"applied": True, "state": "succeeded"}, (status, result)

        final = runtime.db.get_job(job_id)
        assert final["state"] == "succeeded", final["state"]
        assert final.get("assistant_text") == "final answer", repr(final.get("assistant_text"))

        usage_rows = [e for e in runtime.db.list_usage_events() if e.get("job_id") == job_id]
        assert len(usage_rows) == 1, usage_rows
        assert usage_rows[0]["tokens_prompt"] == 111 and usage_rows[0]["tokens_output"] == 22, usage_rows[0]
        assert usage_rows[0]["metadata"]["measures"]["cached_input_tokens"] == 7, usage_rows[0]["metadata"]
        assert usage_rows[0]["metadata"]["byok_token_counts_billable"] is False, usage_rows[0]["metadata"]

        audits = [a for a in runtime.db.list_audit(limit=200) if a["resource_id"] == job_id and a["action"] == "job.succeeded"]
        assert audits and all(a["actor"] == "system:worker-callback" for a in audits), audits

        # The structural callback_result event stores names/counters only.
        events = runtime.db.get_job_events_after(job_id, 0, limit=1000)
        result_events = [e for e in events if e["event_type"] == "callback_result"]
        assert len(result_events) == 1, result_events
        assert result_events[0]["payload"]["tool_calls"] == ["Read", "Bash"], result_events[0]
        assert "summary" not in result_events[0]["payload"], result_events[0]

        # Duplicate delivery (thClaws retries): idempotent no-op with 200 so the worker stops.
        status, result = _post_callback(envelope["url"], token, _terminal_payload(job_id))
        assert status == 200 and result == {"applied": False, "state": "succeeded"}, (status, result)
        final = runtime.db.get_job(job_id)
        assert final.get("assistant_text") == "final answer", "duplicate delivery double-applied summary"
        assert len([e for e in runtime.db.list_usage_events() if e.get("job_id") == job_id]) == 1
        terminal_states = [
            e for e in runtime.db.get_job_events_after(job_id, 0, limit=1000)
            if e["event_type"] == "state" and e["payload"].get("state") in {"succeeded", "failed", "cancelled"}
        ]
        assert len(terminal_states) == 1, terminal_states
    finally:
        server.shutdown()
        server.server_close()
        mock.shutdown()
        mock.server_close()


def check_rejected_tokens(tmp: Path) -> None:
    """Tampered / expired / cross-job / missing tokens → 401, the job stays untouched, and a
    job.callback_rejected audit row (system actor) is written."""
    mock, worker_base = _start_mock_worker()
    runtime = _make_runtime(tmp, "rejects")
    server, _base = _start_atlas(runtime)
    try:
        job = _submit_callback_job(runtime, worker_base, "reject cases")
        job_id = job["id"]
        envelope = CAPTURED[job_id]
        url = envelope["url"]
        good_token = envelope["api_key"]

        tampered = good_token[:-1] + ("0" if good_token[-1] != "0" else "1")
        expired = mint_callback_token(job_id, int(time.time()) - 10, SECRET)
        cross_job = mint_callback_token("job_other", int(time.time()) + 3600, SECRET)
        for label, bad in (("tampered", tampered), ("expired", expired), ("cross-job", cross_job), ("missing", None)):
            status, _body = _post_callback(url, bad, _terminal_payload(job_id))
            assert status == 401, f"{label} token must 401, got {status}"

        current = runtime.db.get_job(job_id)
        assert current["state"] == "running", f"rejected callbacks must not touch the job: {current['state']}"
        # Rate-limited durable auditing: four rapid rejections against the SAME real job write
        # exactly ONE audit row (a compromised worker knows real job ids, so per-request rows
        # would let it grow the DB/WAL without bound — one row per job per window keeps the
        # security signal without the amplification).
        rejected = [a for a in runtime.db.list_audit(limit=200) if a["action"] == "job.callback_rejected" and a["resource_id"] == job_id]
        assert len(rejected) == 1 and rejected[0]["actor"] == "system:worker-callback", rejected

        # Valid token still works afterwards (rejections consumed nothing).
        status, result = _post_callback(url, good_token, _terminal_payload(job_id))
        assert status == 200 and result["applied"] is True, (status, result)
    finally:
        server.shutdown()
        server.server_close()
        mock.shutdown()
        mock.server_close()


def check_token_validity_envelope(tmp: Path) -> None:
    """Simulated clock: the token minted at dispatch must still verify at the callback deadline
    PLUS the worker's full retry envelope (3 attempts, ~160s worst case) — and must fail past
    its embedded expiry. Removing the envelope margin from minting turns this red."""
    mock, worker_base = _start_mock_worker()
    runtime = _make_runtime(tmp, "envelope")
    server, _base = _start_atlas(runtime)
    try:
        dispatch_epoch = time.time()
        job = _submit_callback_job(runtime, worker_base, "token envelope")
        job_id = job["id"]
        token = CAPTURED[job_id]["api_key"]

        expires_epoch = int(token.partition(".")[0])
        timeout = runtime.jobs.callback_timeout_seconds
        worker_retry_window = 160  # 0/10/60s backoff + 30s per-attempt request timeout
        deadline_epoch = dispatch_epoch + timeout
        assert expires_epoch >= deadline_epoch + worker_retry_window, (
            f"token expiry {expires_epoch} must cover deadline {deadline_epoch:.0f} + {worker_retry_window}s retry envelope"
        )
        assert expires_epoch <= dispatch_epoch + timeout + CALLBACK_RETRY_ENVELOPE_SECONDS + 5, "expiry unexpectedly far out"

        # Boundary probes with an injected clock — legitimate final retry accepted...
        assert verify_callback_token(job_id, token, SECRET, now_epoch=deadline_epoch + worker_retry_window)
        # ...and anything past the signed expiry rejected.
        assert not verify_callback_token(job_id, token, SECRET, now_epoch=expires_epoch + 1)
    finally:
        server.shutdown()
        server.server_close()
        mock.shutdown()
        mock.server_close()


def check_oversized_body(tmp: Path) -> None:
    """A body over the cap is rejected BEFORE any byte is read, even with a valid token — the
    pre-auth surface must never buffer an unbounded body. Because the server answers 413 and
    closes without draining, the client may see the response OR a connection reset mid-send;
    both count as rejected-before-read. The job stays untouched and the endpoint stays healthy."""
    mock, worker_base = _start_mock_worker()
    runtime = _make_runtime(tmp, "oversize")
    server, _base = _start_atlas(runtime)
    try:
        job = _submit_callback_job(runtime, worker_base, "oversized body")
        job_id = job["id"]
        envelope = CAPTURED[job_id]
        huge = b"x" * (_CALLBACK_MAX_BODY_BYTES + 1)
        try:
            status, _body = _post_callback(envelope["url"], envelope["api_key"], None, raw_body=huge)
        except urllib.error.URLError as exc:
            assert isinstance(exc.reason, (ConnectionResetError, BrokenPipeError)), exc.reason
        else:
            assert status == 413, f"oversized callback body must be rejected, got {status}"
        assert (runtime.db.get_job(job_id) or {}).get("state") == "running", "oversized body must not touch the job"
        # The rejection consumed nothing: a normal-size valid delivery still lands.
        status, result = _post_callback(envelope["url"], envelope["api_key"], _terminal_payload(job_id))
        assert status == 200 and result["applied"] is True, (status, result)
    finally:
        server.shutdown()
        server.server_close()
        mock.shutdown()
        mock.server_close()


def check_reaper_fires_at_deadline(tmp: Path) -> None:
    """A callback job that never calls back is failed by the reaper once its deadline passes —
    and a job whose deadline is far out is NOT touched by the same sweep."""
    mock, worker_base = _start_mock_worker()
    db = Database(tmp / "reaper.sqlite")
    fast = JobManager(db, request_timeout_seconds=5, public_base_url="http://127.0.0.1:1", secret_key=SECRET)
    fast.callback_timeout_seconds = 1
    fast.callback_reap_grace_seconds = 0  # test deadline-firing itself; the grace has its own check
    slow = JobManager(db, request_timeout_seconds=5, public_base_url="http://127.0.0.1:1", secret_key=SECRET)
    slow.callback_timeout_seconds = 3600
    try:
        worker = db.upsert_worker({"base_url": worker_base, "name": "reaper-mock"})
        due = fast.submit({"prompt": "due job", "worker_id": worker["id"], "execution": "callback"})
        pending = slow.submit({"prompt": "pending job", "worker_id": worker["id"], "execution": "callback"})
        _wait(lambda: due["id"] in CAPTURED and pending["id"] in CAPTURED, message="both dispatches")

        time.sleep(2.5)  # 1s deadline (second-resolution timestamps) safely in the past
        reaped = fast.reap_callback_jobs()
        assert reaped == 1, f"exactly the due job must be reaped, got {reaped}"

        failed = db.get_job(due["id"])
        assert failed["state"] == "failed", failed["state"]
        assert "deadline" in (failed.get("error") or ""), failed.get("error")
        usage_rows = [e for e in db.list_usage_events() if e.get("job_id") == due["id"]]
        assert len(usage_rows) == 1 and usage_rows[0]["status"] == "failed", usage_rows
        audits = [a for a in db.list_audit(limit=200) if a["resource_id"] == due["id"] and a["action"] == "job.failed"]
        assert audits and audits[0]["actor"] == "system:callback-reaper", audits

        untouched = db.get_job(pending["id"])
        assert untouched["state"] == "running", f"undue callback job must survive the sweep: {untouched['state']}"
    finally:
        mock.shutdown()
        mock.server_close()


def check_reaper_honors_retry_grace(tmp: Path) -> None:
    """A job whose deadline just passed must NOT be reaped while the retry-envelope grace is
    still open — its callback token stays valid for the worker's retries, and reaping now would
    turn an authenticated retry into a lost result. It is reaped only once deadline + grace has
    elapsed. Default grace = CALLBACK_RETRY_ENVELOPE_SECONDS."""
    mock, worker_base = _start_mock_worker()
    db = Database(tmp / "reapgrace.sqlite")
    manager = JobManager(db, request_timeout_seconds=5, public_base_url="http://127.0.0.1:1", secret_key=SECRET)
    manager.callback_timeout_seconds = 1
    assert manager.callback_reap_grace_seconds == float(CALLBACK_RETRY_ENVELOPE_SECONDS), manager.callback_reap_grace_seconds
    try:
        worker = db.upsert_worker({"base_url": worker_base, "name": "grace-mock"})
        job = manager.submit({"prompt": "grace job", "worker_id": worker["id"], "execution": "callback"})
        _wait(lambda: job["id"] in CAPTURED, message="dispatch")
        time.sleep(2.5)  # deadline (1s) is now in the past, but the 300s grace is still open
        assert manager.reap_callback_jobs() == 0, "must not reap within the retry-envelope grace"
        assert db.get_job(job["id"])["state"] == "running", "job must stay open through the grace"

        # A late-but-valid retry within the grace still applies (the token is still valid).
        result = manager.apply_worker_callback(job["id"], _terminal_payload(job["id"]))
        assert result["applied"] is True and result["state"] == "succeeded", result

        # With the grace collapsed, the same overdue shape IS reaped.
        job2 = manager.submit({"prompt": "grace job 2", "worker_id": worker["id"], "execution": "callback"})
        _wait(lambda: job2["id"] in CAPTURED, message="dispatch 2")
        time.sleep(2.5)
        manager.callback_reap_grace_seconds = 0
        assert manager.reap_callback_jobs() == 1, "past deadline + grace must reap"
        assert db.get_job(job2["id"])["state"] == "failed"
    finally:
        mock.shutdown()
        mock.server_close()


def check_callback_vs_reaper_race(tmp: Path) -> None:
    """The realistic terminal race: a late callback and the deadline reaper firing together.
    Both go through the atomic apply_job_terminal_result transition, so exactly one wins — one terminal
    state, one usage row, one terminal state event — in both serial orders AND concurrently."""
    mock, worker_base = _start_mock_worker()
    db = Database(tmp / "race.sqlite")
    manager = JobManager(db, request_timeout_seconds=5, public_base_url="http://127.0.0.1:1", secret_key=SECRET)
    manager.callback_timeout_seconds = 1
    manager.callback_reap_grace_seconds = 0
    try:
        worker = db.upsert_worker({"base_url": worker_base, "name": "race-mock"})

        def _due_job(prompt: str) -> str:
            job = manager.submit({"prompt": prompt, "worker_id": worker["id"], "execution": "callback"})
            _wait(lambda: job["id"] in CAPTURED, message="dispatch")
            return job["id"]

        def _assert_converged(job_id: str) -> str:
            job = db.get_job(job_id)
            assert job["state"] in {"succeeded", "failed"}, job["state"]
            usage_rows = [e for e in db.list_usage_events() if e.get("job_id") == job_id]
            assert len(usage_rows) == 1, usage_rows
            terminal_events = [
                e for e in db.get_job_events_after(job_id, 0, limit=1000)
                if e["event_type"] == "state" and e["payload"].get("state") in {"succeeded", "failed", "cancelled"}
            ]
            assert len(terminal_events) == 1, terminal_events
            return job["state"]

        # Serial order 1: reaper first — the late callback must no-op, not flip the state.
        job_a = _due_job("race serial reaper-first")
        time.sleep(2.5)
        assert manager.reap_callback_jobs() >= 1
        result = manager.apply_worker_callback(job_a, _terminal_payload(job_a))
        assert result["applied"] is False and result["state"] == "failed", result
        assert _assert_converged(job_a) == "failed"

        # Serial order 2: callback first — the sweep must find nothing to reap.
        job_b = _due_job("race serial callback-first")
        time.sleep(2.5)
        result = manager.apply_worker_callback(job_b, _terminal_payload(job_b))
        assert result["applied"] is True, result
        assert manager.reap_callback_jobs() == 0, "reaper must not double-terminalize a delivered job"
        assert _assert_converged(job_b) == "succeeded"

        # Concurrent: both fire through a start barrier; exactly one applies.
        job_c = _due_job("race concurrent")
        time.sleep(2.5)
        barrier = threading.Barrier(2)
        outcome: dict[str, Any] = {}

        def _deliver() -> None:
            barrier.wait()
            outcome["callback"] = manager.apply_worker_callback(job_c, _terminal_payload(job_c))

        def _reap() -> None:
            barrier.wait()
            outcome["reaped"] = manager.reap_callback_jobs()

        threads = [threading.Thread(target=_deliver), threading.Thread(target=_reap)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        callback_won = outcome["callback"]["applied"] is True
        reaper_won = outcome["reaped"] >= 1
        assert callback_won != reaper_won, f"exactly one writer must win: {outcome}"
        _assert_converged(job_c)
    finally:
        mock.shutdown()
        mock.server_close()


def check_restart_preserves_callback_pending(tmp: Path) -> None:
    """Atlas restart with a callback-pending job: reconcile must NOT fail it (it is running
    remotely, not interrupted) — while an orphaned STREAM job in the same DB IS failed — and a
    late callback after the restart still completes it."""
    mock, worker_base = _start_mock_worker()
    runtime1 = _make_runtime(tmp, "restart")
    server1, _base1 = _start_atlas(runtime1)
    job_id = None
    try:
        job = _submit_callback_job(runtime1, worker_base, "survives restart")
        job_id = job["id"]
        # An orphaned stream job in the same DB — the reconcile exemption must be scoped to
        # callback-pending jobs only, so this one still fails on restart.
        worker = runtime1.db.upsert_worker({"base_url": "http://127.0.0.1:9", "name": "stale-stream"})
        stream_job = runtime1.db.create_job({"worker_id": worker["id"], "prompt": "orphan", "state": "queued"})
        runtime1.db.update_job(stream_job["id"], state="running", started_at="2026-01-01T00:00:00Z")
    finally:
        server1.shutdown()
        server1.server_close()

    # "Restart": a fresh runtime over the same DB runs reconcile_jobs in __init__.
    runtime2 = AtlasRuntime(
        Config(
            host="127.0.0.1", port=0, db_path=tmp / "restart.sqlite", api_token=None,
            request_timeout_seconds=5, enable_loopback_without_token=False,
            secret_key=SECRET, upload_dir=tmp / "restart-uploads",
            public_base_url="http://atlas.invalid",
        )
    )
    server2, _base2 = _start_atlas(runtime2)
    try:
        survived = runtime2.db.get_job(job_id)
        assert survived["state"] == "running", f"callback-pending job must survive reconcile, got {survived['state']}"
        assert not survived.get("error"), survived.get("error")
        reconciled = runtime2.db.get_job(stream_job["id"])
        assert reconciled["state"] == "failed", f"orphaned stream job must still reconcile to failed, got {reconciled['state']}"

        # The original pre-restart token still authorizes the late delivery on the new server.
        envelope = CAPTURED[job_id]
        url = f"http://127.0.0.1:{server2.server_address[1]}/api/worker-callbacks/{job_id}"
        status, result = _post_callback(url, envelope["api_key"], _terminal_payload(job_id, summary="late but fine"))
        assert status == 200 and result == {"applied": True, "state": "succeeded"}, (status, result)
        assert runtime2.db.get_job(job_id)["assistant_text"] == "late but fine"
    finally:
        server2.shutdown()
        server2.server_close()
        mock.shutdown()
        mock.server_close()


def check_restart_reconciles_jobs_beyond_history_window(tmp: Path) -> None:
    """Restart recovery must inspect every non-terminal job, even when 10,000 newer
    terminal rows would fill the dashboard history query."""
    db = Database(tmp / "restart-history.sqlite")
    worker = db.upsert_worker({"base_url": "http://127.0.0.1:9", "name": "history-worker"})
    orphan = db.create_job({
        "worker_id": worker["id"],
        "prompt": "crashed before callback deadline",
        "state": "queued",
        "execution": "callback",
    })
    with db.connect() as conn:
        conn.execute(
            "UPDATE jobs SET created_at = '2000-01-01T00:00:00Z', updated_at = created_at WHERE id = ?",
            (orphan["id"],),
        )
        conn.executemany(
            "INSERT INTO jobs(id, worker_id, state, prompt, created_at, updated_at) "
            "VALUES (?, ?, 'succeeded', 'history', '2099-01-01T00:00:00Z', '2099-01-01T00:00:00Z')",
            ((f"job_history_{index:05d}", worker["id"]) for index in range(10_000)),
        )
        plan = " ".join(
            str(dict(row)) for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM jobs "
                "WHERE state NOT IN ('succeeded', 'failed', 'cancelled')"
            ).fetchall()
        )

    assert "idx_jobs_non_terminal" in plan, f"restart recovery query must use the live-job index: {plan}"

    JobManager(db).reconcile_jobs()

    recovered = db.get_job(orphan["id"])
    assert recovered["state"] == "failed", f"old orphan was skipped by restart recovery: {recovered}"


def check_dispatch_failure_terminal(tmp: Path) -> None:
    """A callback job whose dispatch POST never reaches the worker must go terminal 'failed'
    WITH a usage ledger row — the dispatch thread records usage only when it terminal-izes the
    job itself (on a clean ACK the callback/reaper owns usage, so a NULL-token row from this
    thread can never shadow the worker's real token counts)."""
    db = Database(tmp / "dispatchfail.sqlite")
    manager = JobManager(db, request_timeout_seconds=2, public_base_url="http://127.0.0.1:1", secret_key=SECRET)
    worker = db.upsert_worker({"base_url": "http://127.0.0.1:9", "name": "unreachable"})
    job = manager.submit({"prompt": "x", "worker_id": worker["id"], "execution": "callback"})
    _wait(lambda: (db.get_job(job["id"]) or {}).get("state") in {"succeeded", "failed", "cancelled"}, message="dispatch failure")
    final = db.get_job(job["id"])
    assert final["state"] == "failed", final["state"]
    usage_rows = [e for e in db.list_usage_events() if e.get("job_id") == job["id"]]
    assert len(usage_rows) == 1 and usage_rows[0]["status"] == "failed", usage_rows
    # Metering uses a REFRESHED job row: the pre-start snapshot has started_at NULL, which
    # would record a null start/duration on the immutable usage row.
    assert usage_rows[0]["started_at"] is not None and usage_rows[0]["seconds"] is not None, usage_rows[0]


def check_cancel_races_callback_terminal_write(tmp: Path) -> None:
    """A cancel that lands before the callback's terminal write must WIN: the terminal
    apply honors cancel_requested inside the same atomic UPDATE, so the callback converges to
    'cancelled' (with the cancel honored in audit/usage), never 'succeeded' with a dangling
    cancel_requested flag. The reaper path converts the same way."""
    mock, worker_base = _start_mock_worker()
    db = Database(tmp / "cancelrace.sqlite")
    manager = JobManager(db, request_timeout_seconds=5, public_base_url="http://127.0.0.1:1", secret_key=SECRET)
    try:
        worker = db.upsert_worker({"base_url": worker_base, "name": "cancel-race"})
        job = manager.submit({"prompt": "cancel me", "worker_id": worker["id"], "execution": "callback"})
        _wait(lambda: job["id"] in CAPTURED, message="dispatch")
        assert db.mark_cancel_requested(job["id"]) is True
        result = manager.apply_worker_callback(job["id"], _terminal_payload(job["id"], status="succeeded"))
        assert result == {"applied": True, "state": "cancelled"}, result
        final = db.get_job(job["id"])
        assert final["state"] == "cancelled" and final.get("error") is None, final
        usage_rows = [e for e in db.list_usage_events() if e.get("job_id") == job["id"]]
        assert len(usage_rows) == 1 and usage_rows[0]["status"] == "cancelled", usage_rows

        # Reaper path: a due, cancel-requested job is reaped as 'cancelled', not 'failed'.
        manager.callback_timeout_seconds = 1
        manager.callback_reap_grace_seconds = 0
        job2 = manager.submit({"prompt": "cancel then reap", "worker_id": worker["id"], "execution": "callback"})
        _wait(lambda: job2["id"] in CAPTURED, message="dispatch 2")
        db.mark_cancel_requested(job2["id"])
        time.sleep(2.5)
        assert manager.reap_callback_jobs() == 1
        reaped = db.get_job(job2["id"])
        assert reaped["state"] == "cancelled" and reaped.get("error") is None, reaped
        # The reaper asked for 'failed' with a deadline-exceeded reason, but cancel_requested
        # flipped it to 'cancelled': the stale failure reason must NOT leak into the recorded
        # outcome (state event / audit), or a cancel reads as a deadline failure.
        cancel_events = [
            e for e in db.get_job_events_after(job2["id"], 0, limit=1000)
            if e["event_type"] == "state" and e["payload"].get("state") == "cancelled"
        ]
        assert cancel_events and all("deadline" not in str(e["payload"]) for e in cancel_events), cancel_events
        cancel_audits = [
            a for a in db.list_audit(limit=500)
            if a["resource_id"] == job2["id"] and a["action"] == "job.cancelled"
        ]
        assert cancel_audits and all("deadline" not in str(a.get("details")) for a in cancel_audits), cancel_audits
    finally:
        mock.shutdown()
        mock.server_close()


class AcceptThenDropWorker(BaseHTTPRequestHandler):
    """Accepts the /agent/run POST (captures the envelope — 'the run started') but then drops
    the connection WITHOUT sending the 202 ACK: the ambiguous-dispatch shape where the worker
    is running with a valid token while Atlas never saw the acceptance."""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        callback = request.get("x_callback") or {}
        if callback.get("run_id"):
            CAPTURED[callback["run_id"]] = callback
        # Close without any response bytes -> client sees a connection error, not an HTTP error.
        self.connection.close()


def check_ambiguous_dispatch_stays_pending(tmp: Path) -> None:
    """An x_callback POST whose ACK is lost (network drop after the worker accepted) must NOT
    fail the job: the worker holds a valid token, so the job stays callback-pending
    (callback_dispatch_unconfirmed event) and the real delivery — or the reaper — resolves it.
    Failing here would discard the worker's future result. Contrast: a definitive HTTP
    rejection (EchoRejectWorker below) still fails the job."""
    mock = ThreadingHTTPServer(("127.0.0.1", 0), AcceptThenDropWorker)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    db = Database(tmp / "ambiguous.sqlite")
    manager = JobManager(db, request_timeout_seconds=5, public_base_url="http://127.0.0.1:1", secret_key=SECRET)
    try:
        host, port = mock.server_address
        worker = db.upsert_worker({"base_url": f"http://{host}:{port}", "name": "accept-drop"})
        job = manager.submit({"prompt": "ambiguous ack", "worker_id": worker["id"], "execution": "callback"})
        _wait(lambda: job["id"] in CAPTURED, message="envelope capture")
        _wait(
            lambda: any(
                e["event_type"] == "callback_dispatch_unconfirmed"
                for e in db.get_job_events_after(job["id"], 0, limit=1000)
            ),
            message="unconfirmed-dispatch event",
        )
        pending = db.get_job(job["id"])
        assert pending["state"] == "running", f"ambiguous dispatch must stay callback-pending, got {pending['state']}"
        assert pending.get("callback_deadline_at"), pending

        # The worker's (real) delivery still lands and completes the job.
        result = manager.apply_worker_callback(job["id"], _terminal_payload(job["id"], summary="made it anyway"))
        assert result == {"applied": True, "state": "succeeded"}, result
        assert db.get_job(job["id"])["assistant_text"] == "made it anyway"
    finally:
        mock.shutdown()
        mock.server_close()


class EchoRejectWorker(BaseHTTPRequestHandler):
    """Rejects /agent/run with a 400 whose body ECHOES the whole request — the hostile-proxy /
    verbose-validation shape that would reflect the x_callback api_key back inside the error."""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(HTTPStatus.BAD_REQUEST)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def check_dispatch_error_redacts_token(tmp: Path) -> None:
    """A dispatch error whose body echoes the request must NOT persist the callback token: the
    error string lands in jobs.error, a job event, and audit, so the token is redacted at the
    dispatch boundary. Byte-scan the DB (and WAL) for zero occurrences."""
    mock = ThreadingHTTPServer(("127.0.0.1", 0), EchoRejectWorker)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    db = Database(tmp / "redact.sqlite")
    manager = JobManager(db, request_timeout_seconds=5, public_base_url="http://127.0.0.1:1", secret_key=SECRET)
    try:
        host, port = mock.server_address
        worker = db.upsert_worker({"base_url": f"http://{host}:{port}", "name": "echo-reject"})
        job = manager.submit({"prompt": "leak probe", "worker_id": worker["id"], "execution": "callback"})
        _wait(lambda: (db.get_job(job["id"]) or {}).get("state") in {"succeeded", "failed", "cancelled"}, message="echoed dispatch failure")
        final = db.get_job(job["id"])
        assert final["state"] == "failed", final["state"]
        error_text = final.get("error") or ""
        assert "x_callback" in error_text, f"echoed error body expected in jobs.error: {error_text!r}"
        assert "[redacted-callback-token]" in error_text, error_text
        # The signed token has the shape <epoch>.<64-hex>; assert no such value survived
        # anywhere in the DB files (error string, events, audit).
        for suffix in ("", "-wal"):
            path = Path(str(db.path) + suffix)
            if path.exists():
                blob = path.read_bytes().decode("utf-8", errors="replace")
                assert not re.search(r"\b\d{10}\.[0-9a-f]{64}\b", blob), f"callback token leaked into {path.name}"
    finally:
        mock.shutdown()
        mock.server_close()


def check_workflow_recovery_marks_callback_pending(tmp: Path) -> None:
    """A workflow run restarted mid-node keeps the standing explicit-recovery posture — BUT when
    the node's job is callback-pending (still running remotely), the recovery entry must say so:
    callback_pending flag on the interrupted item, a node error naming the remote job, and the
    operator warning telling them to check the job outcome before a duplicate-risk retry."""
    db = Database(tmp / "wfrecovery.sqlite")
    worker = db.upsert_worker({"base_url": "http://127.0.0.1:9", "name": "wf-callback"})
    graph = {
        "start": "a",
        "nodes": [{"id": "a", "type": "worker", "prompt": "p", "worker_id": worker["id"], "execution": "callback"}],
        "edges": [],
    }
    definition = db.create_workflow_definition({"name": "Callback recovery", "graph": graph})
    run = db.create_workflow_run(
        {
            "workflow_definition_id": definition["id"],
            "state": "running",
            "current_nodes": ["a"],
            "counters": {"jobs_started": 1, "node_counts": {"a": 1}, "completed_nodes": [], "failed_nodes": []},
            "started_at": "2026-01-01T00:00:00Z",
        }
    )
    pending_job = db.create_job({"worker_id": worker["id"], "prompt": "p", "state": "queued", "execution": "callback"})
    db.update_job(pending_job["id"], state="running", started_at="2026-01-01T00:00:00Z", callback_deadline_at="2099-01-01T00:00:00Z")
    runtime_node = db.create_workflow_node(
        {"run_id": run["id"], "node_key": "a", "state": "running", "attempt": 1, "job_id": pending_job["id"]}
    )

    manager = JobManager(db)
    manager.reconcile_jobs()  # must NOT touch the callback-pending job
    assert db.get_job(pending_job["id"])["state"] == "running"

    WorkflowRunner(db, manager).reconcile_runs()
    recovered = db.get_workflow_run(run["id"])
    assert recovered["state"] == "recovery_required", recovered["state"]
    entry = (recovered["counters"].get("recovery") or {}).get("interrupted")[0]
    assert entry["callback_pending"] is True and entry["job_id"] == pending_job["id"], entry
    assert "callback" in (db.get_workflow_node(runtime_node["id"]).get("error") or ""), db.get_workflow_node(runtime_node["id"])
    warning = (recovered["counters"].get("recovery") or {}).get("warning") or ""
    assert "callback" in warning and "NEW job" in warning, warning
    # Contrast: a stream-job node in another run keeps the plain interrupted entry.
    run2 = db.create_workflow_run(
        {
            "workflow_definition_id": definition["id"],
            "state": "running",
            "current_nodes": ["a"],
            "counters": {"jobs_started": 1, "node_counts": {"a": 1}, "completed_nodes": [], "failed_nodes": []},
            "started_at": "2026-01-01T00:00:00Z",
        }
    )
    stream_job = db.create_job({"worker_id": worker["id"], "prompt": "p", "state": "queued"})
    db.update_job(stream_job["id"], state="running", started_at="2026-01-01T00:00:00Z")
    db.create_workflow_node({"run_id": run2["id"], "node_key": "a", "state": "running", "attempt": 1, "job_id": stream_job["id"]})
    manager.reconcile_jobs()  # stream orphan fails as before
    assert db.get_job(stream_job["id"])["state"] == "failed"
    WorkflowRunner(db, manager).reconcile_runs()
    recovered2 = db.get_workflow_run(run2["id"])
    entry2 = (recovered2["counters"].get("recovery") or {}).get("interrupted")[0]
    assert entry2["callback_pending"] is False, entry2


class Mock200Worker(BaseHTTPRequestHandler):
    """Answers /agent/run with 200 {} — an incompatible worker or proxy that never scheduled
    an async run. Only a 202 is the x_callback contract's acceptance signal."""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = b"{}"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def check_non_202_ack_fails_fast(tmp: Path) -> None:
    """A 2xx that is NOT the contract's 202 ACK proves no async run was scheduled — the job
    must fail fast with a clear error instead of parking callback-pending until the reaper."""
    mock = ThreadingHTTPServer(("127.0.0.1", 0), Mock200Worker)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    db = Database(tmp / "ack200.sqlite")
    manager = JobManager(db, request_timeout_seconds=5, public_base_url="http://127.0.0.1:1", secret_key=SECRET)
    try:
        host, port = mock.server_address
        worker = db.upsert_worker({"base_url": f"http://{host}:{port}", "name": "two-hundred"})
        job = manager.submit({"prompt": "not a real ack", "worker_id": worker["id"], "execution": "callback"})
        _wait(lambda: (db.get_job(job["id"]) or {}).get("state") in {"succeeded", "failed", "cancelled"}, message="fast failure on 200 ACK")
        final = db.get_job(job["id"])
        assert final["state"] == "failed", final["state"]
        assert "202" in (final.get("error") or ""), final.get("error")
    finally:
        mock.shutdown()
        mock.server_close()


class WrongAckWorker(BaseHTTPRequestHandler):
    """202-ACKs with a NON-conforming body (status/run_id do not echo the contract): the run
    was accepted per the status code, but the ACK must not be trusted for session binding or
    recorded as a clean dispatch."""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        callback = request.get("x_callback") or {}
        if callback.get("run_id"):
            CAPTURED[callback["run_id"]] = callback
        body = json.dumps({"status": "accepted", "run_id": "run-of-someone-else", "session_id": "sess-evil"}).encode("utf-8")
        self.send_response(HTTPStatus.ACCEPTED)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def check_mismatched_ack_stays_pending(tmp: Path) -> None:
    """A 202 whose body does not echo OUR run_id (or status:'accepted') is a non-conforming
    intermediary: the job stays callback-pending on the AMBIGUOUS path (unconfirmed event, no
    session binding from the untrusted echo, no callback_dispatched record) and the real
    delivery still completes it."""
    mock = ThreadingHTTPServer(("127.0.0.1", 0), WrongAckWorker)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    db = Database(tmp / "wrongack.sqlite")
    manager = JobManager(db, request_timeout_seconds=5, public_base_url="http://127.0.0.1:1", secret_key=SECRET)
    try:
        host, port = mock.server_address
        worker = db.upsert_worker({"base_url": f"http://{host}:{port}", "name": "wrong-ack"})
        job = manager.submit({"prompt": "mismatched ack", "worker_id": worker["id"], "execution": "callback"})
        _wait(
            lambda: any(
                e["event_type"] == "callback_dispatch_unconfirmed"
                for e in db.get_job_events_after(job["id"], 0, limit=1000)
            ),
            message="unconfirmed event after mismatched ACK",
        )
        events = {e["event_type"] for e in db.get_job_events_after(job["id"], 0, limit=1000)}
        assert "callback_dispatched" not in events, "mismatched ACK must not record a clean dispatch"
        current = db.get_job(job["id"])
        assert current["state"] == "running", current["state"]
        assert current.get("thclaws_session_id") != "sess-evil", "session must not bind from an untrusted ACK echo"
        result = manager.apply_worker_callback(job["id"], _terminal_payload(job["id"]))
        assert result["applied"] is True, result
    finally:
        mock.shutdown()
        mock.server_close()


def check_payload_validation(tmp: Path) -> None:
    """Callback payload validation with a VALID token: a present run_id that does not match
    the URL's job id is a 400 that touches nothing (delivery mix-up must not apply run B's
    result to job A); a non-string status maps tolerantly to 'failed', never a TypeError→500
    that leaves the job running."""
    mock, worker_base = _start_mock_worker()
    runtime = _make_runtime(tmp, "payloadval")
    server, _base = _start_atlas(runtime)
    try:
        job = _submit_callback_job(runtime, worker_base, "payload validation")
        job_id = job["id"]
        envelope = CAPTURED[job_id]

        mismatched = _terminal_payload("job_someone_else")
        status, _body = _post_callback(envelope["url"], envelope["api_key"], mismatched)
        assert status == 400, f"mismatched run_id must 400, got {status}"
        assert (runtime.db.get_job(job_id) or {}).get("state") == "running", "mismatched run_id must not touch the job"

        # The documented payload REQUIRES run_id == job_id; a missing / null / empty / non-string
        # run_id is rejected too (not silently accepted), and touches nothing.
        for bad_run_id in (None, "", 123, ["x"]):
            body = _terminal_payload(job_id)
            if bad_run_id is None:
                body.pop("run_id")
            else:
                body["run_id"] = bad_run_id
            status, _body = _post_callback(envelope["url"], envelope["api_key"], body)
            assert status == 400, f"run_id={bad_run_id!r} must 400, got {status}"
            assert (runtime.db.get_job(job_id) or {}).get("state") == "running", f"run_id={bad_run_id!r} must not touch the job"

        weird = _terminal_payload(job_id)
        weird["status"] = {"weird": True}
        status, result = _post_callback(envelope["url"], envelope["api_key"], weird)
        assert status == 200 and result == {"applied": True, "state": "failed"}, (status, result)
        final = runtime.db.get_job(job_id)
        # A non-string (unhashable) status is unrecognized → failed with the offending value
        # surfaced verbatim, not a silent mystery failure.
        assert "unrecognized terminal status" in (final.get("error") or ""), final.get("error")
    finally:
        server.shutdown()
        server.server_close()
        mock.shutdown()
        mock.server_close()


def check_unrecognized_status_fails_loudly(tmp: Path) -> None:
    """A callback whose status is a STRING outside the pinned enum (succeeded|failed|cancelled)
    — worker/protocol drift, or an additive upstream value — must map to 'failed', NEVER
    silently 'succeeded' (which would hand a possibly-failed run downstream), and must surface
    the raw value verbatim so the mismatch is diagnosable. A recognized 'succeeded' still passes
    through (the guard did not over-reject). Mutation: map an unknown status to 'succeeded', or
    drop the verbatim surfacing, and this goes red."""
    mock, worker_base = _start_mock_worker()
    runtime = _make_runtime(tmp, "unknownstatus")
    server, _base = _start_atlas(runtime)
    try:
        job = _submit_callback_job(runtime, worker_base, "unknown status")
        envelope = CAPTURED[job["id"]]
        # thClaws today sends "succeeded"; a drift to e.g. "completed" must fail loudly, not
        # silently succeed a run whose real outcome Atlas cannot confirm.
        status, result = _post_callback(
            envelope["url"], envelope["api_key"], _terminal_payload(job["id"], status="completed")
        )
        assert status == 200 and result == {"applied": True, "state": "failed"}, (status, result)
        final = runtime.db.get_job(job["id"])
        assert final["state"] == "failed", final["state"]
        assert "unrecognized terminal status" in (final.get("error") or ""), final.get("error")
        assert "completed" in (final.get("error") or ""), final.get("error")

        # The guard is not over-broad: a recognized 'succeeded' still applies as success.
        job2 = _submit_callback_job(runtime, worker_base, "known status")
        env2 = CAPTURED[job2["id"]]
        status, result = _post_callback(env2["url"], env2["api_key"], _terminal_payload(job2["id"], status="succeeded"))
        assert status == 200 and result == {"applied": True, "state": "succeeded"}, (status, result)
    finally:
        server.shutdown()
        server.server_close()
        mock.shutdown()
        mock.server_close()


def check_usage_context_backfill(tmp: Path) -> None:
    """The node→job link (update_workflow_node with job_id) repairs a usage row whose
    attribution is NULL — a fast job (callback, or an instant stream worker) can write its
    idempotent usage row BEFORE the runner links the node, and that row is otherwise
    unrepairable. Non-NULL attribution is never touched."""
    db = Database(tmp / "backfill.sqlite")
    worker = db.upsert_worker({"base_url": "http://127.0.0.1:9", "name": "backfill"})
    definition = db.create_workflow_definition(
        {"name": "Backfill", "graph": {"start": "a", "nodes": [{"id": "a", "type": "worker", "prompt": "p", "worker_id": worker["id"]}], "edges": []}}
    )
    run = db.create_workflow_run({"workflow_definition_id": definition["id"], "state": "running", "started_at": "2026-01-01T00:00:00Z"})
    node = db.create_workflow_node({"run_id": run["id"], "node_key": "a", "state": "running", "attempt": 1})

    fast_job = db.create_job({"worker_id": worker["id"], "prompt": "p", "state": "queued"})
    db.emit_usage_event({"idempotency_key": f"job:{fast_job['id']}", "kind": "job", "job_id": fast_job["id"], "run_id": None})
    other_job = db.create_job({"worker_id": worker["id"], "prompt": "q", "state": "queued"})
    db.emit_usage_event({"idempotency_key": f"job:{other_job['id']}", "kind": "job", "job_id": other_job["id"], "run_id": "run_other", "node_key": "z"})

    db.update_workflow_node(node["id"], job_id=fast_job["id"])
    repaired = next(e for e in db.list_usage_events() if e.get("job_id") == fast_job["id"])
    assert repaired["run_id"] == run["id"] and repaired["node_key"] == "a", repaired
    untouched = next(e for e in db.list_usage_events() if e.get("job_id") == other_job["id"])
    assert untouched["run_id"] == "run_other" and untouched["node_key"] == "z", untouched


def check_callback_wait_grace_math(tmp: Path) -> None:
    """The runner's callback-wait extension = time-to-deadline + the reaper grace (unit-tested
    on the pure helper so the grace term is locked without minute-scale real time). A callback
    node's wait is derived from the job service's OWN reap grace (single source of truth with
    the reaper), so the runner never gives up before the reaper would."""
    now = 1_000_000.0
    # deadline 50s out, grace 300 → must wait 350s more.
    deadline_50s = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 50))
    assert _callback_wait_extra_seconds(deadline_50s, 300.0, now) == 350.0
    # the grace term must be INCLUDED, not dropped (locks the "ignore grace" mutation).
    assert _callback_wait_extra_seconds(deadline_50s, 0.0, now) == 50.0
    assert _callback_wait_extra_seconds(deadline_50s, 300.0, now) > _callback_wait_extra_seconds(deadline_50s, 120.0, now)
    # past deadline or unparseable → no extension.
    past = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10))
    assert _callback_wait_extra_seconds(past, 300.0, now) is None
    assert _callback_wait_extra_seconds("not-a-date", 300.0, now) is None

    # The runner reads the grace from the job service (so it tracks the reaper's grace, not a
    # hardcoded constant): a manager with a distinctive reap grace flows into the wait.
    db = Database(tmp / "waitgrace.sqlite")
    manager = JobManager(db, public_base_url="http://127.0.0.1:1", secret_key=SECRET)
    manager.callback_reap_grace_seconds = 777.0
    runner = WorkflowRunner(db, manager)
    assert getattr(runner.job_service, "callback_reap_grace_seconds") == 777.0
    assert _CALLBACK_WAIT_SWEEP_MARGIN_SECONDS > 0


def check_workflow_wait_extends_for_callback(tmp: Path) -> None:
    """A workflow node whose callback job legitimately outlives the runner's fixed max_wait
    cap must NOT be cancelled early: the wait extends to the job's own callback deadline, so
    the run completes when the (late) callback lands."""
    mock, worker_base = _start_mock_worker()
    db = Database(tmp / "wfwait.sqlite")
    manager = JobManager(db, request_timeout_seconds=5, public_base_url="http://127.0.0.1:1", secret_key=SECRET)
    manager.callback_timeout_seconds = 30  # deadline far beyond the tiny runner cap below
    runner = WorkflowRunner(db, manager, max_wait_seconds=0.2, poll_interval_seconds=0.02)
    try:
        worker = db.upsert_worker({"base_url": worker_base, "name": "wf-wait"})
        definition = db.create_workflow_definition(
            {
                "name": "Callback wait",
                "graph": {
                    "start": "a",
                    "nodes": [{"id": "a", "type": "worker", "prompt": "long job", "worker_id": worker["id"], "execution": "callback", "outputs": ["answer"]}],
                    "edges": [],
                },
            }
        )
        run = runner.start_workflow(definition["id"], {})

        def _deliver() -> None:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                node_rows = db.list_workflow_nodes(run["id"])
                job_id = node_rows[0].get("job_id") if node_rows else None
                if job_id and job_id in CAPTURED:
                    time.sleep(1.0)  # well past the 0.2s runner cap — only the extension keeps it waiting
                    manager.apply_worker_callback(job_id, _terminal_payload(job_id, summary="took a while"))
                    return
                time.sleep(0.02)

        deliverer = threading.Thread(target=_deliver)
        deliverer.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current = db.get_workflow_run(run["id"])
            if current and current["state"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.05)
        deliverer.join(timeout=10)
        final = db.get_workflow_run(run["id"])
        assert final["state"] == "succeeded", f"callback node must outlive the runner wait cap, got {final['state']}: {final.get('error')}"
    finally:
        mock.shutdown()
        mock.server_close()


def check_apply_is_atomic(tmp: Path) -> None:
    """The callback apply is ONE transaction: if any derived write fails mid-apply (poisoned
    event payload here — a stand-in for a crash or DB error), the terminal transition must
    roll back with it, leaving the job NON-terminal so the worker's retry re-applies the full
    result. A split transaction would leave the job terminal with the result lost forever."""
    db = Database(tmp / "atomic.sqlite")
    worker = db.upsert_worker({"base_url": "http://127.0.0.1:9", "name": "atomic"})
    job = db.create_job({"worker_id": worker["id"], "prompt": "p", "state": "queued", "execution": "callback"})
    db.update_job(job["id"], state="running", started_at="2026-01-01T00:00:00Z")

    poisoned = [("callback_result", {"bad": {1, 2, 3}})]  # a set is not JSON-serializable
    try:
        db.apply_job_terminal_result(job["id"], "succeeded", summary="must not survive", events=poisoned)
    except TypeError:
        pass
    else:
        raise AssertionError("poisoned event payload must raise")
    after = db.get_job(job["id"])
    assert after["state"] == "running", f"failed apply must roll back the terminal state, got {after['state']}"
    assert (after.get("assistant_text") or "") == "", "failed apply must roll back the summary text"
    assert not db.get_job_events_after(job["id"], 0, limit=10), "failed apply must roll back its events"
    assert not [e for e in db.list_usage_events() if e.get("job_id") == job["id"]], "failed apply must roll back usage"

    # The retry (clean payload) then applies EVERYTHING.
    final = db.apply_job_terminal_result(job["id"], "succeeded", summary="second try lands")
    assert final == "succeeded"
    retried = db.get_job(job["id"])
    assert retried["state"] == "succeeded" and retried["assistant_text"] == "second try lands", retried


class Reject500Worker(BaseHTTPRequestHandler):
    """Answers /agent/run with a 500 — the proxy-502/worker-500 family that can arrive AFTER
    the request was accepted, so it must NOT terminal-ize the job."""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = b'{"error": "backend exploded"}'
        self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def check_5xx_dispatch_stays_pending(tmp: Path) -> None:
    """A 5xx answer to the dispatch POST (proxy 502/504, worker 500) is AMBIGUOUS — the run
    may have been scheduled before the error — so the job must stay callback-pending, not
    fail. Contrast: the 4xx EchoRejectWorker path (validated rejection) still fails it."""
    mock = ThreadingHTTPServer(("127.0.0.1", 0), Reject500Worker)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    db = Database(tmp / "fivehundred.sqlite")
    manager = JobManager(db, request_timeout_seconds=5, public_base_url="http://127.0.0.1:1", secret_key=SECRET)
    try:
        host, port = mock.server_address
        worker = db.upsert_worker({"base_url": f"http://{host}:{port}", "name": "reject-500"})
        job = manager.submit({"prompt": "5xx dispatch", "worker_id": worker["id"], "execution": "callback"})
        _wait(
            lambda: any(
                e["event_type"] == "callback_dispatch_unconfirmed"
                for e in db.get_job_events_after(job["id"], 0, limit=1000)
            ),
            message="unconfirmed event after 5xx",
        )
        current = db.get_job(job["id"])
        assert current["state"] == "running", f"5xx dispatch must stay pending, got {current['state']}"
        # The (possibly scheduled) run's delivery still lands.
        result = manager.apply_worker_callback(job["id"], _terminal_payload(job["id"]))
        assert result["applied"] is True and result["state"] == "succeeded", result
    finally:
        mock.shutdown()
        mock.server_close()


class BigAckWorker(BaseHTTPRequestHandler):
    """202-ACKs /agent/run with a VALID-JSON body far over the ACK cap: without a bounded
    read, one worker could feed the dispatch thread gigabytes; with it, the oversized ACK is
    just an ambiguous dispatch (the 2xx means the run was accepted)."""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        callback = request.get("x_callback") or {}
        if callback.get("run_id"):
            CAPTURED[callback["run_id"]] = callback
        body = json.dumps({"session_id": "sess-big", "pad": "x" * (512 * 1024)}).encode("utf-8")
        self.send_response(HTTPStatus.ACCEPTED)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def check_oversized_ack_bounded(tmp: Path) -> None:
    """An ACK body over the cap must abort the bounded read and land on the AMBIGUOUS path
    (unconfirmed event, job stays pending — the 2xx already accepted the run; the callback
    completes it). An unbounded read would instead parse the giant ACK and record a normal
    dispatch — that contrast is what the mutation flips."""
    mock = ThreadingHTTPServer(("127.0.0.1", 0), BigAckWorker)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    db = Database(tmp / "bigack.sqlite")
    manager = JobManager(db, request_timeout_seconds=5, public_base_url="http://127.0.0.1:1", secret_key=SECRET)
    try:
        host, port = mock.server_address
        worker = db.upsert_worker({"base_url": f"http://{host}:{port}", "name": "big-ack"})
        job = manager.submit({"prompt": "big ack", "worker_id": worker["id"], "execution": "callback"})
        _wait(
            lambda: any(
                e["event_type"] == "callback_dispatch_unconfirmed"
                for e in db.get_job_events_after(job["id"], 0, limit=1000)
            ),
            message="unconfirmed event after oversized ACK",
        )
        events = {e["event_type"] for e in db.get_job_events_after(job["id"], 0, limit=1000)}
        assert "callback_dispatched" not in events, "oversized ACK must not be parsed as a normal dispatch"
        current = db.get_job(job["id"])
        assert current["state"] == "running", current["state"]
        result = manager.apply_worker_callback(job["id"], _terminal_payload(job["id"]))
        assert result["applied"] is True, result
    finally:
        mock.shutdown()
        mock.server_close()


def check_body_read_deadline(tmp: Path) -> None:
    """A token-holding worker that declares a Content-Length and then DRIPS the body (each
    byte inside the per-recv window, resetting it) must be cut by the wall-clock read deadline
    — otherwise it pins one handler thread per connection indefinitely. The endpoint stays
    healthy for the next legitimate delivery."""
    import atlas.app as app_module
    import socket as socket_module

    mock, worker_base = _start_mock_worker()
    runtime = _make_runtime(tmp, "bodydrip")
    server, base = _start_atlas(runtime)
    original_recv = app_module._CALLBACK_BODY_RECV_TIMEOUT_SECONDS
    original_deadline = app_module._CALLBACK_BODY_READ_DEADLINE_SECONDS
    app_module._CALLBACK_BODY_RECV_TIMEOUT_SECONDS = 0.5
    app_module._CALLBACK_BODY_READ_DEADLINE_SECONDS = 1.0
    try:
        job = _submit_callback_job(runtime, worker_base, "body drip")
        job_id = job["id"]
        envelope = CAPTURED[job_id]

        port = server.server_address[1]
        raw = socket_module.create_connection(("127.0.0.1", port), timeout=15)
        try:
            headers = (
                f"POST /api/worker-callbacks/{job_id} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Authorization: Bearer {envelope['api_key']}\r\n"
                "Content-Type: application/json\r\n"
                "Content-Length: 200\r\n\r\n"
            )
            raw.sendall(headers.encode("ascii"))
            start = time.monotonic()

            def _drip() -> None:
                try:
                    for _ in range(100):  # ~30s of drip if nobody cuts it
                        raw.sendall(b"x")
                        time.sleep(0.3)
                except OSError:
                    return  # server cut the connection — expected

            dripper = threading.Thread(target=_drip, daemon=True)
            dripper.start()
            raw.settimeout(10)
            response = raw.recv(4096)  # deadline must answer long before the drip finishes
            elapsed = time.monotonic() - start
            # 503 (retryable), NOT 408: thClaws abandons delivery on any non-429 4xx, so a
            # transient body stall must stay retryable or the completed job is lost to the reaper.
            assert b"503" in response.split(b"\r\n", 1)[0], response[:100]
            assert elapsed < 5, f"read deadline must cut the drip promptly, took {elapsed:.1f}s"
        finally:
            raw.close()
        assert (runtime.db.get_job(job_id) or {}).get("state") == "running", "timed-out body must not touch the job"
        status, result = _post_callback(envelope["url"], envelope["api_key"], _terminal_payload(job_id))
        assert status == 200 and result["applied"] is True, (status, result)
    finally:
        app_module._CALLBACK_BODY_RECV_TIMEOUT_SECONDS = original_recv
        app_module._CALLBACK_BODY_READ_DEADLINE_SECONDS = original_deadline
        server.shutdown()
        server.server_close()
        mock.shutdown()
        mock.server_close()


def check_reject_audit_race(tmp: Path) -> None:
    """Concurrent invalid callbacks for ONE real job must produce exactly ONE audit row: the
    window check-and-reserve is serialized, so racing handler threads cannot all observe the
    stale timestamp and each write a durable row."""
    mock, worker_base = _start_mock_worker()
    runtime = _make_runtime(tmp, "auditrace")
    server, _base = _start_atlas(runtime)
    try:
        job = _submit_callback_job(runtime, worker_base, "audit race")
        job_id = job["id"]
        url = CAPTURED[job_id]["url"]
        bad = mint_callback_token(job_id, int(time.time()) - 10, SECRET)
        barrier = threading.Barrier(8)

        def _reject() -> None:
            barrier.wait()
            status, _body = _post_callback(url, bad, _terminal_payload(job_id))
            assert status == 401, status

        threads = [threading.Thread(target=_reject) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        rows = [a for a in runtime.db.list_audit(limit=500) if a["action"] == "job.callback_rejected" and a["resource_id"] == job_id]
        assert len(rows) == 1, f"racing rejections must write exactly one audit row, got {len(rows)}"
    finally:
        server.shutdown()
        server.server_close()
        mock.shutdown()
        mock.server_close()


def check_attribution_inside_terminal_transaction(tmp: Path) -> None:
    """When the node→job link already exists, the terminal apply derives workflow attribution
    IN-transaction even though the caller pre-read no context — closing the interleaving where
    the context read predates the link but the usage insert postdates it."""
    db = Database(tmp / "attrtxn.sqlite")
    worker = db.upsert_worker({"base_url": "http://127.0.0.1:9", "name": "attr"})
    definition = db.create_workflow_definition(
        {"name": "Attr", "graph": {"start": "a", "nodes": [{"id": "a", "type": "worker", "prompt": "p", "worker_id": worker["id"]}], "edges": []}}
    )
    run = db.create_workflow_run({"workflow_definition_id": definition["id"], "state": "running", "started_at": "2026-01-01T00:00:00Z"})
    job = db.create_job({"worker_id": worker["id"], "prompt": "p", "state": "queued", "execution": "callback"})
    db.update_job(job["id"], state="running", started_at="2026-01-01T00:00:00Z")
    db.create_workflow_node({"run_id": run["id"], "node_key": "a", "state": "running", "attempt": 1, "job_id": job["id"]})

    # The caller's payload carries NO attribution (as if its context pre-read lost the race).
    final = db.apply_job_terminal_result(
        job["id"], "succeeded",
        usage_payload={"idempotency_key": f"job:{job['id']}", "kind": "job", "job_id": job["id"], "run_id": None},
    )
    assert final == "succeeded"
    row = next(e for e in db.list_usage_events() if e.get("job_id") == job["id"])
    assert row["run_id"] == run["id"] and row["node_key"] == "a", row


def check_reaper_query_indexed(tmp: Path) -> None:
    """The due-callback sweep runs every few seconds forever, so it must be an index lookup —
    and the partial index must EXCLUDE terminal jobs (its predicate carries the state filter),
    or every completed callback stays indexed forever and the sweep grows with history."""
    db = Database(tmp / "reaperidx.sqlite")
    with db.connect() as conn:
        ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'idx_jobs_callback_due'").fetchone()
        assert ddl and "state NOT IN" in ddl[0], f"partial index must exclude terminal jobs: {ddl}"
        plan = " ".join(
            str(dict(r)) for r in conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM jobs WHERE execution = 'callback' "
                "AND callback_deadline_at IS NOT NULL AND callback_deadline_at <= ? "
                "AND state NOT IN ('succeeded','failed','cancelled')",
                ("2026-01-01T00:00:00Z",),
            ).fetchall()
        )
    assert "idx_jobs_callback_due" in plan, f"reaper query must use the partial index, plan: {plan}"


def check_read_slots_bounded(tmp: Path) -> None:
    """Concurrent callback-body reads are slot-bounded: with every slot held, a valid delivery
    gets a retryable 503 (thClaws retries 5xx) instead of pinning yet another handler thread;
    once a slot frees, the same delivery lands."""
    import atlas.app as app_module

    mock, worker_base = _start_mock_worker()
    runtime = _make_runtime(tmp, "readslots")
    server, _base = _start_atlas(runtime)
    try:
        job = _submit_callback_job(runtime, worker_base, "slot bound")
        job_id = job["id"]
        envelope = CAPTURED[job_id]

        held = 0
        while runtime.callback_read_slots.acquire(blocking=False):
            held += 1
        assert held == app_module._CALLBACK_READ_SLOTS, held
        try:
            status, _body = _post_callback(envelope["url"], envelope["api_key"], _terminal_payload(job_id))
            assert status == 503, f"slot-exhausted delivery must 503 (retryable), got {status}"
            assert (runtime.db.get_job(job_id) or {}).get("state") == "running", "503 must not touch the job"
        finally:
            for _ in range(held):
                runtime.callback_read_slots.release()
        status, result = _post_callback(envelope["url"], envelope["api_key"], _terminal_payload(job_id))
        assert status == 200 and result["applied"] is True, (status, result)
    finally:
        server.shutdown()
        server.server_close()
        mock.shutdown()
        mock.server_close()


def check_audit_cap_fails_closed(tmp: Path) -> None:
    """When the rejection-audit cache is saturated with IN-WINDOW entries, a new rejection
    writes NO durable row (fail closed) — clearing the cache would let a worker rotating >cap
    real job ids reset every window and write without bound. Expired entries are evicted, so
    auditing resumes once windows age out."""
    mock, worker_base = _start_mock_worker()
    runtime = _make_runtime(tmp, "auditcap")
    server, _base = _start_atlas(runtime)
    try:
        job = _submit_callback_job(runtime, worker_base, "cap closed")
        job_id = job["id"]
        url = CAPTURED[job_id]["url"]
        bad = mint_callback_token(job_id, int(time.time()) - 10, SECRET)

        fresh = time.monotonic()
        with runtime.callback_reject_audit_lock:
            runtime.callback_reject_audited_at.clear()
            runtime.callback_reject_audited_at.update({f"job_fake_{i}": fresh for i in range(1024)})
        status, _body = _post_callback(url, bad, _terminal_payload(job_id))
        assert status == 401, status
        rows = [a for a in runtime.db.list_audit(limit=2000) if a["action"] == "job.callback_rejected" and a["resource_id"] == job_id]
        assert not rows, "saturated cache must fail CLOSED, not write a row"
        assert len(runtime.callback_reject_audited_at) >= 1024, "in-window entries must survive saturation"

        stale = fresh - 3600  # every fake window long expired
        with runtime.callback_reject_audit_lock:
            for key in list(runtime.callback_reject_audited_at):
                runtime.callback_reject_audited_at[key] = stale
        status, _body = _post_callback(url, bad, _terminal_payload(job_id))
        assert status == 401, status
        rows = [a for a in runtime.db.list_audit(limit=2000) if a["action"] == "job.callback_rejected" and a["resource_id"] == job_id]
        assert len(rows) == 1, f"expired windows must evict and auditing resume, got {len(rows)} rows"
        assert len(runtime.callback_reject_audited_at) < 1024, "expired entries must be evicted"
    finally:
        server.shutdown()
        server.server_close()
        mock.shutdown()
        mock.server_close()


def check_rejection_audit_bounded(tmp: Path) -> None:
    """Rejected callbacks are audited ONLY when the job id is real: this surface needs no
    credential to reach, so auditing junk requests would let any peer grow the DB/WAL without
    bound. (Rejections against real jobs stay recorded — asserted in check_rejected_tokens.)"""
    mock, worker_base = _start_mock_worker()
    runtime = _make_runtime(tmp, "auditbound")
    server, base = _start_atlas(runtime)
    try:
        ghost = "job_does_not_exist_1234"
        before = len(runtime.db.list_audit(limit=1000))
        for _ in range(5):
            status, _body = _post_callback(
                f"{base}/api/worker-callbacks/{ghost}",
                mint_callback_token(ghost, int(time.time()) - 10, "wrong-secret"),
                _terminal_payload(ghost),
            )
            assert status == 401, status
        audits = runtime.db.list_audit(limit=1000)
        assert len(audits) == before, "junk callback rejections must not write durable audit rows"
        assert not any(a["resource_id"] == ghost for a in audits), audits
    finally:
        server.shutdown()
        server.server_close()
        mock.shutdown()
        mock.server_close()


def check_workflow_run_rejects_unconfigured_callback(tmp: Path) -> None:
    """POST /api/workflow-runs must reject a graph with execution:'callback' nodes at START
    time when async execution is unconfigured — a synchronous 400 with NO run row, instead of
    a 202 run that fails later inside the background node submission."""
    db = Database(tmp / "wfstart.sqlite")
    worker = db.upsert_worker({"base_url": "http://127.0.0.1:9", "name": "wf-start"})
    graph = {
        "start": "a",
        "nodes": [{"id": "a", "type": "worker", "prompt": "p", "worker_id": worker["id"], "execution": "callback"}],
        "edges": [],
    }
    definition = db.create_workflow_definition({"name": "Unconfigured callback", "graph": graph})

    unconfigured = WorkflowRunner(db, JobManager(db))  # no public_base_url / secret_key
    before = len(db.list_workflow_runs(limit=100))
    try:
        unconfigured.start_workflow(definition["id"], {})
    except ValueError as exc:
        assert "ATLAS_PUBLIC_BASE_URL" in str(exc), str(exc)
    else:
        raise AssertionError("start_workflow must reject callback nodes when async execution is unconfigured")
    assert len(db.list_workflow_runs(limit=100)) == before, "rejected start must not create a run"

    configured = WorkflowRunner(db, JobManager(db, public_base_url="http://127.0.0.1:1", secret_key=SECRET))
    configured._validate_callback_nodes_supported(graph)  # passes: both preconditions present
    # A callback-free graph is untouched by the gate even when unconfigured.
    unconfigured._validate_callback_nodes_supported({"nodes": [{"id": "a", "type": "worker", "prompt": "p"}]})


class TokenEchoAckWorker(BaseHTTPRequestHandler):
    """A hostile worker that echoes the callback token (the api_key it received) back in the
    202 ACK's session_id — an attempt to get the LIVE credential persisted where a
    read-authorized user could retrieve it and forge the terminal callback."""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        body = json.dumps({"ok": True} if self.path == "/healthz" else {"name": "echo"}).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        callback = request.get("x_callback") or {}
        token = callback.get("api_key")
        if callback.get("run_id"):
            CAPTURED[callback["run_id"]] = callback
        ack = {"run_id": callback.get("run_id"), "status": "accepted", "session_id": token}  # session_id == token
        body = json.dumps(ack).encode("utf-8")
        self.send_response(HTTPStatus.ACCEPTED)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def check_token_not_persisted_from_worker_fields(tmp: Path) -> None:
    """The callback token is a LIVE credential. A semi-trusted worker echoing it into ANY
    persisted worker-controlled field must not leak it: (1) an ACK session_id equal to the
    token is not bound (no session id, no binding, byte-scan clean); (2) a terminal callback
    whose summary, model, and tool_calls contain the token stores none — byte-scan the DB file."""
    mock = ThreadingHTTPServer(("127.0.0.1", 0), TokenEchoAckWorker)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    runtime = _make_runtime(tmp, "tokenfields")
    server, _base = _start_atlas(runtime)
    try:
        host, port = mock.server_address
        worker = runtime.db.upsert_worker({"base_url": f"http://{host}:{port}", "name": "echo"})
        job = runtime.jobs.submit({"prompt": "echo token", "worker_id": worker["id"], "execution": "callback"})
        _wait(lambda: job["id"] in CAPTURED, message="dispatch")
        token = CAPTURED[job["id"]]["api_key"]

        # (1) The ACK echoed session_id == token; it must NOT be bound or persisted.
        bound = runtime.db.get_job(job["id"]).get("thclaws_session_id")
        assert bound != token, "token bound as the session id"
        for suffix in ("", "-wal"):
            path = Path(str(runtime.db.path) + suffix)
            if path.exists():
                assert token.encode("utf-8") not in path.read_bytes(), f"ACK token leaked into {path.name}"

        # (2) A terminal callback echoing the token in summary + tool names stores neither.
        payload = _terminal_payload(job["id"], summary=f"done, key was {token}")
        payload["model"] = f"model-{token}"
        payload["tool_calls"] = ["Read", f"Bash({token})"]
        status, result = _post_callback(CAPTURED[job["id"]]["url"], token, payload)
        assert status == 200 and result["applied"] is True, (status, result)
        final = runtime.db.get_job(job["id"])
        assert token not in (final.get("assistant_text") or ""), "token leaked into assistant_text"
        for suffix in ("", "-wal"):
            path = Path(str(runtime.db.path) + suffix)
            if path.exists():
                assert token.encode("utf-8") not in path.read_bytes(), f"terminal token leaked into {path.name}"
    finally:
        server.shutdown()
        server.server_close()
        mock.shutdown()
        mock.server_close()


def check_concurrent_replay_single_handoff(tmp: Path) -> None:
    """Two duplicate callbacks racing on a succeeded source with an unstarted handoff must
    start exactly ONE child — the check-then-submit is serialized, so the replay-recovery path
    cannot spawn duplicate handoff jobs (duplicate worker side effects)."""
    db = Database(tmp / "handoffrace.sqlite")
    manager = JobManager(db, request_timeout_seconds=2, public_base_url="http://127.0.0.1:1", secret_key=SECRET)
    src_worker = db.upsert_worker({"base_url": "http://127.0.0.1:9", "name": "src"})
    dst_worker = db.upsert_worker({"base_url": "http://127.0.0.1:8", "name": "dst"})
    job = db.create_job({
        "worker_id": src_worker["id"], "prompt": "p", "state": "queued", "execution": "callback",
        "handoff_worker_id": dst_worker["id"],
    })
    db.update_job(job["id"], state="succeeded", started_at="2026-01-01T00:00:00Z", finished_at="2026-01-01T00:00:01Z")
    db.append_job_text(job["id"], "source text")

    barrier = threading.Barrier(6)

    def _replay() -> None:
        barrier.wait()
        manager.apply_worker_callback(job["id"], _terminal_payload(job["id"]))

    threads = [threading.Thread(target=_replay) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    children = [j for j in db.list_jobs(limit=1000) if j.get("parent_job_id") == job["id"]]
    assert len(children) == 1, f"concurrent replays must start exactly one handoff child, got {len(children)}"
    assert db.get_job(job["id"])["handoff_job_id"] == children[0]["id"], "handoff_job_id must point at the single child"
    _await_threads(manager)


def check_terminal_error_redacts_token(tmp: Path) -> None:
    """A worker callback with status:failed whose error.message reflects the callback token
    (the Bearer it received) must NOT persist that token: apply redacts the verified token from
    jobs.error, the error event, and audit — the never-store-tokens invariant holds on the
    failure path too. Byte-scan the DB (and WAL) for zero occurrences."""
    mock, worker_base = _start_mock_worker()
    runtime = _make_runtime(tmp, "termredact")
    server, _base = _start_atlas(runtime)
    try:
        job = _submit_callback_job(runtime, worker_base, "terminal redact")
        job_id = job["id"]
        envelope = CAPTURED[job_id]
        token = envelope["api_key"]
        payload = _terminal_payload(job_id, status="failed")
        payload["error"] = {"code": "agent_error", "message": f"boom while using api_key={token} for callback"}
        status, result = _post_callback(envelope["url"], token, payload)
        assert status == 200 and result == {"applied": True, "state": "failed"}, (status, result)
        final = runtime.db.get_job(job_id)
        assert "[redacted-callback-token]" in (final.get("error") or ""), final.get("error")
        assert token not in (final.get("error") or ""), "token leaked into jobs.error"
        for suffix in ("", "-wal"):
            path = Path(str(runtime.db.path) + suffix)
            if path.exists():
                assert token.encode("utf-8") not in path.read_bytes(), f"token leaked into {path.name}"
    finally:
        server.shutdown()
        server.server_close()
        mock.shutdown()
        mock.server_close()


def check_handoff_resumes_on_replay(tmp: Path) -> None:
    """Crash after the terminal commit but before the handoff: the job is succeeded with a
    configured-but-unstarted handoff. The worker's retry (a replay that loses the terminal
    race) must resume the handoff — otherwise the source stays succeeded with no child. A
    plain duplicate (handoff already started) stays a no-op."""
    db = Database(tmp / "handoffreplay.sqlite")
    manager = JobManager(db, request_timeout_seconds=2, public_base_url="http://127.0.0.1:1", secret_key=SECRET)
    source_worker = db.upsert_worker({"base_url": "http://127.0.0.1:9", "name": "src"})
    handoff_worker = db.upsert_worker({"base_url": "http://127.0.0.1:8", "name": "dst"})
    # Simulate the post-crash state: a callback job already committed 'succeeded' with a
    # handoff target set but handoff_job_id still NULL (the handoff call never ran).
    job = db.create_job({
        "worker_id": source_worker["id"], "prompt": "p", "state": "queued", "execution": "callback",
        "handoff_worker_id": handoff_worker["id"],
    })
    db.update_job(job["id"], state="succeeded", started_at="2026-01-01T00:00:00Z", finished_at="2026-01-01T00:00:01Z")
    db.append_job_text(job["id"], "source result text")  # non-empty → handoff proceeds

    result = manager.apply_worker_callback(job["id"], _terminal_payload(job["id"]))
    assert result["applied"] is False and result["state"] == "succeeded", result
    _wait(lambda: db.get_job(job["id"]).get("handoff_job_id"), message="handoff resumed on replay")
    child_id = db.get_job(job["id"])["handoff_job_id"]
    assert child_id, "replay must start the unstarted handoff"

    # A SECOND replay is a no-op — handoff already started, no duplicate child.
    manager.apply_worker_callback(job["id"], _terminal_payload(job["id"]))
    assert db.get_job(job["id"])["handoff_job_id"] == child_id, "duplicate replay must not restart the handoff"
    _await_threads(manager)


def _await_threads(manager: JobManager, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and manager._threads:
        time.sleep(0.01)


class DribbleErrorWorker(BaseHTTPRequestHandler):
    """Answers /agent/run dispatch with a 400 declaring a large body, then DRIBBLES it slowly
    forever (each byte inside the socket window). A byte cap alone would not stop this — only a
    wall-clock deadline on the error-body read cuts it."""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def finish(self) -> None:
        try:
            super().finish()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(HTTPStatus.BAD_REQUEST)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(10 * 1024 * 1024))
        self.end_headers()
        try:
            for _ in range(1000):
                self.wfile.write(b"e")
                self.wfile.flush()
                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


class WithholdErrorBodyWorker(BaseHTTPRequestHandler):
    """Sends 400 error headers declaring a body, then WITHHOLDS it — read1 blocks on the
    socket. Distinguishes the per-read deadline cap (cut at ~deadline) from relying on the
    inherited request timeout (cut at ~request_timeout, which is larger here)."""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def finish(self) -> None:
        try:
            super().finish()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(HTTPStatus.BAD_REQUEST)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "4096")
        self.end_headers()
        try:
            time.sleep(15)  # withhold the body; outlast request_timeout so only the per-read cap saves us
        except Exception:
            pass


def check_error_body_read_capped_per_tick(tmp: Path) -> None:
    """A worker that sends error headers then withholds the body must be cut at the error-body
    READ DEADLINE (~1s), not the larger request timeout (8s): each recv is capped at the
    remaining deadline. Without the per-read cap one read blocks the whole request timeout."""
    import atlas.thclaws_client as client_module

    mock = ThreadingHTTPServer(("127.0.0.1", 0), WithholdErrorBodyWorker)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    db = Database(tmp / "withholderr.sqlite")
    manager = JobManager(db, request_timeout_seconds=8, public_base_url="http://127.0.0.1:1", secret_key=SECRET)
    original = client_module._ERROR_BODY_READ_DEADLINE_SECONDS
    client_module._ERROR_BODY_READ_DEADLINE_SECONDS = 1.0
    try:
        host, port = mock.server_address
        worker = db.upsert_worker({"base_url": f"http://{host}:{port}", "name": "withhold-error"})
        start = time.monotonic()
        job = manager.submit({"prompt": "withhold error", "worker_id": worker["id"], "execution": "callback"})
        _wait(lambda: (db.get_job(job["id"]) or {}).get("state") in {"succeeded", "failed", "cancelled"}, timeout=12, message="deadline-capped error read")
        elapsed = time.monotonic() - start
        assert db.get_job(job["id"])["state"] == "failed", db.get_job(job["id"])["state"]
        assert elapsed < 5, f"error-body read must be cut at the ~1s deadline, not the 8s request timeout; took {elapsed:.1f}s"
    finally:
        client_module._ERROR_BODY_READ_DEADLINE_SECONDS = original
        mock.shutdown()
        mock.server_close()


def check_error_body_deadline(tmp: Path) -> None:
    """A 4xx error body that dribbles must be cut by the error-body READ DEADLINE, not just the
    byte cap: the dispatch fails (4xx definitive) promptly with a truncated error, no thread
    pinned for the full body. Distinguishes byte-cap-only (would keep reading dribbled bytes up
    to 64 KiB over ~50 min) from a deadline bound (cuts in ~1 s)."""
    import atlas.thclaws_client as client_module

    mock = ThreadingHTTPServer(("127.0.0.1", 0), DribbleErrorWorker)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    db = Database(tmp / "dribbleerr.sqlite")
    manager = JobManager(db, request_timeout_seconds=5, public_base_url="http://127.0.0.1:1", secret_key=SECRET)
    original = client_module._ERROR_BODY_READ_DEADLINE_SECONDS
    client_module._ERROR_BODY_READ_DEADLINE_SECONDS = 1.0
    try:
        host, port = mock.server_address
        worker = db.upsert_worker({"base_url": f"http://{host}:{port}", "name": "dribble-error"})
        start = time.monotonic()
        job = manager.submit({"prompt": "dribble error", "worker_id": worker["id"], "execution": "callback"})
        _wait(lambda: (db.get_job(job["id"]) or {}).get("state") in {"succeeded", "failed", "cancelled"}, timeout=8, message="deadline-cut error dispatch")
        elapsed = time.monotonic() - start
        final = db.get_job(job["id"])
        assert final["state"] == "failed", final["state"]
        assert "truncated" in (final.get("error") or ""), final.get("error")
        assert elapsed < 5, f"error-body read must be cut at the ~1s deadline, took {elapsed:.1f}s"
    finally:
        client_module._ERROR_BODY_READ_DEADLINE_SECONDS = original
        mock.shutdown()
        mock.server_close()


def check_public_id_cannot_hijack_handoff(tmp: Path) -> None:
    """POST /api/jobs passes its body straight to submit(); a body `id` must be IGNORED so a
    caller cannot pre-create a job at another job's deterministic handoff id and hijack the
    handoff linkage. The explicit_id path is private (keyword-only)."""
    db = Database(tmp / "hijack.sqlite")
    manager = JobManager(db, request_timeout_seconds=2, public_base_url="http://127.0.0.1:1", secret_key=SECRET)
    worker = db.upsert_worker({"base_url": "http://127.0.0.1:9", "name": "w"})
    dst = db.upsert_worker({"base_url": "http://127.0.0.1:8", "name": "dst"})

    # A public submit that TRIES to set a specific id must not get it — the id is server-assigned.
    forged = manager.submit({"id": "job_attacker_chosen", "prompt": "hi", "worker_id": worker["id"]})
    assert forged["id"] != "job_attacker_chosen", "public submit must not honor a body-supplied id"
    _await_threads(manager)

    # End-to-end: an attacker pre-creates a job at the victim source's deterministic handoff id.
    source = db.create_job({
        "worker_id": worker["id"], "prompt": "p", "state": "queued", "execution": "callback",
        "handoff_worker_id": dst["id"],
    })
    hijack_id = _handoff_child_id(source["id"])
    manager.submit({"id": hijack_id, "prompt": "attacker job", "worker_id": worker["id"]})
    _await_threads(manager)
    # The attacker's job did NOT land at the deterministic id (id was ignored), so the real
    # handoff still runs and creates its own child.
    assert db.get_job(hijack_id) is None, "attacker must not be able to occupy the deterministic handoff id"


def check_handoff_durable_across_crash(tmp: Path) -> None:
    """Crash after submit() created the handoff child but BEFORE handoff_job_id was written:
    the child already exists at its DETERMINISTIC id, with the source still succeeded and
    handoff_job_id NULL. A callback replay must LINK that existing child — not create a second
    one (which the in-process lock alone cannot prevent across a crash)."""
    db = Database(tmp / "handoffcrash.sqlite")
    manager = JobManager(db, request_timeout_seconds=2, public_base_url="http://127.0.0.1:1", secret_key=SECRET)
    src_worker = db.upsert_worker({"base_url": "http://127.0.0.1:9", "name": "src"})
    dst_worker = db.upsert_worker({"base_url": "http://127.0.0.1:8", "name": "dst"})
    source = db.create_job({
        "worker_id": src_worker["id"], "prompt": "p", "state": "queued", "execution": "callback",
        "handoff_worker_id": dst_worker["id"],
    })
    db.update_job(source["id"], state="succeeded", started_at="2026-01-01T00:00:00Z", finished_at="2026-01-01T00:00:01Z")
    db.append_job_text(source["id"], "source text")
    # Simulate the pre-crash child: created at the deterministic id, but handoff_job_id on the
    # source was never written (crash between submit and the link).
    child_id = _handoff_child_id(source["id"])
    db.create_job({"id": child_id, "worker_id": dst_worker["id"], "prompt": "handoff", "state": "succeeded", "parent_job_id": source["id"]})

    result = manager.apply_worker_callback(source["id"], _terminal_payload(source["id"]))
    assert result["applied"] is False, result
    linked = db.get_job(source["id"])["handoff_job_id"]
    assert linked == child_id, f"replay must LINK the existing deterministic child, got {linked}"
    children = [j for j in db.list_jobs(limit=1000) if j.get("parent_job_id") == source["id"]]
    assert len(children) == 1, f"no second child may be created, got {len(children)}"
    events = db.get_job_events_after(source["id"], 0, limit=1000)
    assert any(e["event_type"] == "handoff_started" and e["payload"].get("recovered") for e in events), "recovery must be marked"
    _await_threads(manager)


class BigErrorBodyWorker(BaseHTTPRequestHandler):
    """Answers /agent/run dispatch with a 400 carrying a MULTI-MEGABYTE body: the shared
    _request error path must bound the read, or a semi-trusted worker exhausts memory / pins
    the thread via the error body (which the success-path ACK bound doesn't cover)."""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def finish(self) -> None:
        # Atlas stops reading after the 64 KiB error-body cap and closes; swallow the broken
        # pipe when we flush the rest of the multi-megabyte body.
        try:
            super().finish()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = b"E" * (3 * 1024 * 1024)
        self.send_response(HTTPStatus.BAD_REQUEST)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


def check_error_body_bounded(tmp: Path) -> None:
    """A huge 4xx dispatch-error body is read BOUNDED (truncated), not slurped whole: the job
    fails (4xx is definitive) with a size-capped error, and the call returns promptly."""
    mock = ThreadingHTTPServer(("127.0.0.1", 0), BigErrorBodyWorker)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    db = Database(tmp / "bigerr.sqlite")
    manager = JobManager(db, request_timeout_seconds=5, public_base_url="http://127.0.0.1:1", secret_key=SECRET)
    try:
        host, port = mock.server_address
        worker = db.upsert_worker({"base_url": f"http://{host}:{port}", "name": "big-error"})
        start = time.monotonic()
        job = manager.submit({"prompt": "big error body", "worker_id": worker["id"], "execution": "callback"})
        _wait(lambda: (db.get_job(job["id"]) or {}).get("state") in {"succeeded", "failed", "cancelled"}, message="bounded error dispatch")
        elapsed = time.monotonic() - start
        final = db.get_job(job["id"])
        assert final["state"] == "failed", final["state"]
        assert len(final.get("error") or "") < 128 * 1024, f"error body must be bounded, got {len(final.get('error') or '')} bytes"
        assert "truncated" in (final.get("error") or ""), final.get("error")
        assert elapsed < 5, f"bounded read must return promptly, took {elapsed:.1f}s"
    finally:
        mock.shutdown()
        mock.server_close()


def _serve_malformed_status_line(server_socket: Any) -> None:
    """Accept one connection, read the request, then reply with a MALFORMED HTTP status line so
    the client raises http.client.BadStatusLine (an HTTPException that is NOT an OSError). The
    request was delivered, so this is a post-send ambiguous failure — the worker may be running."""
    import socket as _socket

    server_socket.settimeout(5)
    try:
        conn, _addr = server_socket.accept()
    except (OSError, _socket.timeout):
        return
    try:
        data = b""
        conn.settimeout(2)
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
        # Read the declared body so the request is fully delivered, then capture x_callback.
        header, _, rest = data.partition(b"\r\n\r\n")
        length = 0
        for line in header.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1].strip())
        body = rest
        while len(body) < length:
            chunk = conn.recv(4096)
            if not chunk:
                break
            body += chunk
        try:
            envelope = json.loads(body.decode("utf-8")).get("x_callback") or {}
            if envelope.get("run_id"):
                CAPTURED[envelope["run_id"]] = envelope
        except (ValueError, AttributeError):
            pass
        conn.sendall(b"HELLO THIS IS NOT HTTP\r\n\r\n")  # malformed status line → BadStatusLine
    except OSError:
        pass
    finally:
        conn.close()


def check_protocol_ack_error_is_ambiguous(tmp: Path) -> None:
    """A protocol-level ACK failure after the request is delivered — a malformed status line
    raises http.client.BadStatusLine, an HTTPException that is NOT an OSError — must leave the
    job callback-pending, not fail it definitively: the worker may have accepted the run, and
    its later callback still completes it. (Locks that HTTPException is treated as ambiguous.)"""
    import socket as _socket

    server_socket = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    server_socket.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    server_socket.bind(("127.0.0.1", 0))
    server_socket.listen(1)
    port = server_socket.getsockname()[1]
    threading.Thread(target=_serve_malformed_status_line, args=(server_socket,), daemon=True).start()
    db = Database(tmp / "badstatus.sqlite")
    manager = JobManager(db, request_timeout_seconds=5, public_base_url="http://127.0.0.1:1", secret_key=SECRET)
    try:
        worker = db.upsert_worker({"base_url": f"http://127.0.0.1:{port}", "name": "bad-status"})
        job = manager.submit({"prompt": "bad status line", "worker_id": worker["id"], "execution": "callback"})
        _wait(
            lambda: any(
                e["event_type"] == "callback_dispatch_unconfirmed"
                for e in db.get_job_events_after(job["id"], 0, limit=1000)
            ),
            message="protocol ACK error (BadStatusLine) → unconfirmed (ambiguous)",
        )
        assert db.get_job(job["id"])["state"] == "running", "protocol ACK error must stay callback-pending, not fail"
        # A later valid callback still completes the (possibly-running) job.
        job_row = db.get_job(job["id"])
        assert job_row.get("callback_deadline_at"), "deadline must be set so the reaper still bounds it"
    finally:
        server_socket.close()


def check_slot_held_through_apply(tmp: Path) -> None:
    """The read slot must be held through the WHOLE of read + parse + APPLY, not released after
    the read. Pause a real delivery INSIDE apply_worker_callback and confirm its slot is still
    occupied: exactly _CALLBACK_READ_SLOTS-1 remain acquirable. If the slot were released after
    the read (the bug), all slots would be free during apply and unbounded concurrent
    processing would be possible."""
    import atlas.app as app_module

    mock, worker_base = _start_mock_worker()
    runtime = _make_runtime(tmp, "slotapply")
    server, _base = _start_atlas(runtime)
    entered = threading.Event()
    proceed = threading.Event()
    original_apply = runtime.jobs.apply_worker_callback

    def _paused_apply(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        proceed.wait(10)
        return original_apply(*args, **kwargs)

    runtime.jobs.apply_worker_callback = _paused_apply  # type: ignore[method-assign]
    try:
        job = _submit_callback_job(runtime, worker_base, "slot apply")
        job_id = job["id"]
        envelope = CAPTURED[job_id]

        deliverer = threading.Thread(target=lambda: _post_callback(envelope["url"], envelope["api_key"], _terminal_payload(job_id)))
        deliverer.start()
        assert entered.wait(5), "delivery never reached apply"
        # One request is parked INSIDE apply. If the slot is held through apply, one slot is
        # occupied → only _CALLBACK_READ_SLOTS-1 are acquirable. If released after the read,
        # all _CALLBACK_READ_SLOTS would be free.
        acquired = 0
        while runtime.callback_read_slots.acquire(blocking=False):
            acquired += 1
        for _ in range(acquired):
            runtime.callback_read_slots.release()
        assert acquired == app_module._CALLBACK_READ_SLOTS - 1, (
            f"slot must stay held through apply: {acquired} free, expected {app_module._CALLBACK_READ_SLOTS - 1}"
        )
        proceed.set()
        deliverer.join(timeout=10)
    finally:
        proceed.set()
        runtime.jobs.apply_worker_callback = original_apply  # type: ignore[method-assign]
        server.shutdown()
        server.server_close()
        mock.shutdown()
        mock.server_close()


class SilentAckWorker(BaseHTTPRequestHandler):
    """Sends the 202 status + headers, then WITHHOLDS the ACK body forever. Exercises the
    per-read socket-timeout cap: the read must be cut at the ACK deadline, not the (larger)
    request timeout."""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def finish(self) -> None:
        # Atlas closes the connection at the ACK deadline while we still hold it open; swallow
        # the resulting broken pipe on flush so the mock stays silent.
        try:
            super().finish()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        callback = request.get("x_callback") or {}
        if callback.get("run_id"):
            CAPTURED[callback["run_id"]] = callback
        # Declare a body, send the headers, then never send it — the dispatch read must not
        # block past the ACK deadline even though the request timeout is larger.
        self.send_response(HTTPStatus.ACCEPTED)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "4096")
        self.end_headers()
        try:
            time.sleep(15)  # outlast request_timeout(10s): with the cap the read is cut at the ~1s deadline, without it at ~10s
        except Exception:
            pass


def check_ack_read_capped_by_deadline(tmp: Path) -> None:
    """With request_timeout (10s) ABOVE the ACK deadline, a worker that sends 202 headers then
    withholds the body must be cut at the deadline (~1s here), not pinned for the full request
    timeout — the dispatch socket timeout is capped by the deadline. The job stays
    callback-pending (post-202 is ambiguous)."""
    import atlas.thclaws_client as client_module

    mock = ThreadingHTTPServer(("127.0.0.1", 0), SilentAckWorker)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    db = Database(tmp / "silentack.sqlite")
    manager = JobManager(db, request_timeout_seconds=10, public_base_url="http://127.0.0.1:1", secret_key=SECRET)
    original = client_module._ACK_READ_DEADLINE_SECONDS
    client_module._ACK_READ_DEADLINE_SECONDS = 1.0
    try:
        host, port = mock.server_address
        worker = db.upsert_worker({"base_url": f"http://{host}:{port}", "name": "silent-ack"})
        start = time.monotonic()
        job = manager.submit({"prompt": "silent ack", "worker_id": worker["id"], "execution": "callback"})
        _wait(
            lambda: any(
                e["event_type"] == "callback_dispatch_unconfirmed"
                for e in db.get_job_events_after(job["id"], 0, limit=1000)
            ),
            timeout=8,
            message="dispatch cut at the ACK deadline",
        )
        elapsed = time.monotonic() - start
        assert elapsed < 5, f"read must be cut at the ~1s deadline, not the 10s request timeout; took {elapsed:.1f}s"
        assert (db.get_job(job["id"]) or {}).get("state") == "running", "silent ACK is ambiguous → job stays pending"
    finally:
        client_module._ACK_READ_DEADLINE_SECONDS = original
        mock.shutdown()
        mock.server_close()


def check_error_body_reader_never_raises(tmp: Path) -> None:
    """_read_error_body is best-effort and must NEVER propagate — including http.client
    protocol errors (IncompleteRead is an HTTPException, not OSError). A response whose read
    raises IncompleteRead must yield a truncated diagnostic string, not escape."""
    import http.client
    from atlas.thclaws_client import _read_error_body

    class _RaisingResponse:
        def read1(self, _n: int) -> bytes:
            raise http.client.IncompleteRead(b"partial", expected=100)

    text = _read_error_body(_RaisingResponse())  # must not raise
    assert isinstance(text, str) and "truncated" in text, text


def check_callback_reject_closes_connection(tmp: Path) -> None:
    """Every pre-body-read rejection on the callback route must close the connection: an unread
    declared body on a keep-alive connection would otherwise be parsed as the next request and
    desync it. Send a raw POST with a non-integer Content-Length and confirm 400 + close."""
    import socket as _socket

    mock, worker_base = _start_mock_worker()
    runtime = _make_runtime(tmp, "rejectclose")
    server, base = _start_atlas(runtime)
    try:
        job = _submit_callback_job(runtime, worker_base, "reject close")
        envelope = CAPTURED[job["id"]]
        port = server.server_address[1]
        raw = _socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            # Non-integer Content-Length → 400 before the body is read. Declare a body and send
            # trailing bytes that must NOT be parsed as a second request.
            body = b"POISON-NEXT-REQUEST"
            request = (
                f"POST /api/worker-callbacks/{job['id']} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Authorization: Bearer {envelope['api_key']}\r\n"
                "Content-Type: application/json\r\n"
                "Content-Length: not-a-number\r\n\r\n"
            ).encode("ascii") + body
            raw.sendall(request)
            raw.settimeout(5)
            response = b""
            closed = False
            while True:
                try:
                    chunk = raw.recv(4096)
                except _socket.timeout:
                    break  # keep-alive held open (the bug): server never closed → poison bytes linger
                if not chunk:
                    closed = True  # server closed the socket after the 400 — the desync fix
                    break
                response += chunk
            assert b"400" in response.split(b"\r\n", 1)[0], response[:120]
            assert closed, "rejection must CLOSE the connection so the unread body can't desync the next request"
        finally:
            raw.close()
        # The endpoint is still healthy for the next (separate) delivery.
        status, result = _post_callback(envelope["url"], envelope["api_key"], _terminal_payload(job["id"]))
        assert status == 200 and result["applied"] is True, (status, result)
    finally:
        server.shutdown()
        server.server_close()
        mock.shutdown()
        mock.server_close()


def check_resolved_handoff_replay_idempotent(tmp: Path) -> None:
    """A succeeded callback job whose handoff was SKIPPED (empty source text → handoff_error
    set, handoff_job_id null) must not re-run recovery on every duplicate callback: replays are
    no-ops, appending NO further handoff events/audit rows. Without the handoff_error guard a
    token-holder could grow the DB with unbounded replay writes."""
    db = Database(tmp / "resolvedhandoff.sqlite")
    manager = JobManager(db, request_timeout_seconds=2, public_base_url="http://127.0.0.1:1", secret_key=SECRET)
    src_worker = db.upsert_worker({"base_url": "http://127.0.0.1:9", "name": "src"})
    dst_worker = db.upsert_worker({"base_url": "http://127.0.0.1:8", "name": "dst"})
    job = db.create_job({
        "worker_id": src_worker["id"], "prompt": "p", "state": "queued", "execution": "callback",
        "handoff_worker_id": dst_worker["id"],
    })
    # Succeeded with NO assistant text → the first handoff attempt skips and sets handoff_error.
    db.update_job(job["id"], state="succeeded", started_at="2026-01-01T00:00:00Z", finished_at="2026-01-01T00:00:01Z")
    manager._maybe_start_handoff(job["id"])  # first (legitimate) attempt → skipped
    assert db.get_job(job["id"]).get("handoff_error"), "empty-text handoff must record handoff_error"

    def _handoff_event_count() -> int:
        return sum(1 for e in db.get_job_events_after(job["id"], 0, limit=1000) if e["event_type"].startswith("handoff"))

    def _handoff_audit_count() -> int:
        return sum(1 for a in db.list_audit(limit=1000) if a["resource_id"] == job["id"] and "handoff" in a["action"])

    events_before, audits_before = _handoff_event_count(), _handoff_audit_count()
    for _ in range(4):
        result = manager.apply_worker_callback(job["id"], _terminal_payload(job["id"]))
        assert result["applied"] is False, result
    assert _handoff_event_count() == events_before, "resolved-handoff replays must append no further events"
    assert _handoff_audit_count() == audits_before, "resolved-handoff replays must append no further audit rows"
    _await_threads(manager)


def check_submit_and_graph_validation(tmp: Path) -> None:
    """execution is validated up front: unknown mode rejected; callback without
    ATLAS_PUBLIC_BASE_URL / ATLAS_SECRET_KEY rejected with an actionable message and no orphan
    conversation; workflow graphs accept execution stream|callback on worker nodes and reject
    anything else; the node payload prep forwards execution to job submit."""
    db = Database(tmp / "validate.sqlite")
    worker = db.upsert_worker({"base_url": "http://127.0.0.1:9", "name": "validate"})

    no_base = JobManager(db, request_timeout_seconds=5, public_base_url=None, secret_key=SECRET)
    no_secret = JobManager(db, request_timeout_seconds=5, public_base_url="http://127.0.0.1:1", secret_key=None)
    cases = [
        (no_base, "callback", "ATLAS_PUBLIC_BASE_URL"),
        (no_secret, "callback", "ATLAS_SECRET_KEY"),
        (no_base, "bogus", "execution"),
        # Unhashable JSON values must get the clean ValueError (-> 400), never a TypeError
        # (-> 500) out of the set-membership probe.
        (no_base, ["callback"], "execution"),
        (no_base, {"mode": "callback"}, "execution"),
        # Explicit falsey values are invalid input, NOT a request for the default — silently
        # coercing them to "stream" would accept garbage the schema documents as an enum error.
        (no_base, "", "execution"),
        (no_base, 0, "execution"),
        (no_base, False, "execution"),
        # Explicit JSON null too: only true ABSENCE selects the default.
        (no_base, None, "execution"),
    ]
    before = len(db.list_conversations())
    for manager, execution, needle in cases:
        try:
            manager.submit({"prompt": "x", "worker_id": worker["id"], "execution": execution})
        except ValueError as exc:
            assert needle in str(exc), (needle, str(exc))
        else:
            raise AssertionError(f"submit must reject execution={execution!r} ({needle})")
    assert len(db.list_conversations()) == before, "rejected async submit must not orphan a conversation"

    # Stream default untouched: a plain submit records execution='stream', no deadline.
    plain = db.create_job({"worker_id": worker["id"], "prompt": "plain", "state": "queued"})
    row = db.get_job(plain["id"])
    assert row.get("execution") == "stream" and row.get("callback_deadline_at") is None, row

    graph = {
        "start": "a",
        "nodes": [{"id": "a", "type": "worker", "prompt": "p", "worker_id": worker["id"], "execution": "callback"}],
        "edges": [],
    }
    validate_workflow_graph(graph, {})
    for bad_execution in ("sync", 123, ["callback"]):
        try:
            validate_workflow_graph(
                {"start": "a", "nodes": [{"id": "a", "type": "worker", "prompt": "p", "execution": bad_execution}], "edges": []}, {}
            )
        except ValueError as exc:
            assert "execution" in str(exc), str(exc)
        else:
            raise AssertionError(f"graph validation must reject node execution {bad_execution!r} with ValueError")

    runner = WorkflowRunner(db, JobManager(db))
    payload = runner._prepare_worker_node_payload(
        {"id": "run_x"}, graph["nodes"][0], {}, {}, {}, graph, {}
    )
    assert payload.get("execution") == "callback", payload
    print("  submit/graph validation ok")


def main() -> None:
    with TemporaryDirectory() as raw_tmp:
        tmp = Path(raw_tmp)
        check_callback_end_to_end(tmp)
        check_rejected_tokens(tmp)
        check_token_validity_envelope(tmp)
        check_oversized_body(tmp)
        check_reaper_fires_at_deadline(tmp)
        check_reaper_honors_retry_grace(tmp)
        check_callback_vs_reaper_race(tmp)
        check_restart_preserves_callback_pending(tmp)
        check_restart_reconciles_jobs_beyond_history_window(tmp)
        check_dispatch_failure_terminal(tmp)
        check_dispatch_error_redacts_token(tmp)
        check_cancel_races_callback_terminal_write(tmp)
        check_ambiguous_dispatch_stays_pending(tmp)
        check_apply_is_atomic(tmp)
        check_5xx_dispatch_stays_pending(tmp)
        check_oversized_ack_bounded(tmp)
        check_non_202_ack_fails_fast(tmp)
        check_mismatched_ack_stays_pending(tmp)
        check_payload_validation(tmp)
        check_unrecognized_status_fails_loudly(tmp)
        check_usage_context_backfill(tmp)
        check_workflow_wait_extends_for_callback(tmp)
        check_callback_wait_grace_math(tmp)
        check_body_read_deadline(tmp)
        check_read_slots_bounded(tmp)
        check_audit_cap_fails_closed(tmp)
        check_reject_audit_race(tmp)
        check_attribution_inside_terminal_transaction(tmp)
        check_reaper_query_indexed(tmp)
        check_rejection_audit_bounded(tmp)
        check_workflow_run_rejects_unconfigured_callback(tmp)
        check_workflow_recovery_marks_callback_pending(tmp)
        check_token_not_persisted_from_worker_fields(tmp)
        check_concurrent_replay_single_handoff(tmp)
        check_terminal_error_redacts_token(tmp)
        check_handoff_resumes_on_replay(tmp)
        check_public_id_cannot_hijack_handoff(tmp)
        check_error_body_bounded(tmp)
        check_error_body_deadline(tmp)
        check_error_body_read_capped_per_tick(tmp)
        check_handoff_durable_across_crash(tmp)
        check_ack_read_capped_by_deadline(tmp)
        check_protocol_ack_error_is_ambiguous(tmp)
        check_slot_held_through_apply(tmp)
        check_error_body_reader_never_raises(tmp)
        check_callback_reject_closes_connection(tmp)
        check_resolved_handoff_replay_idempotent(tmp)
        check_submit_and_graph_validation(tmp)
    print("async jobs check ok")


if __name__ == "__main__":
    main()
