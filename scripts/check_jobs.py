from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.app import AtlasHttpServer, AtlasRuntime
from atlas.config import Config
from atlas.db import Database, now_iso
from atlas.jobs import JobManager

# Flipped by the health check to simulate a healthy vs reachable-but-unhealthy worker.
WORKER_OK = {"value": True}
# Counts /agent/run dispatches so a test can assert a cancelled-while-queued job never hit
# the worker.
DISPATCH_COUNT = {"value": 0}


def _await_threads_drained(manager: JobManager, timeout: float = 3.0) -> None:
    """Wait for all job threads to finish their `finally` (usage write + self-removal) before a
    test's TemporaryDirectory is cleaned up — otherwise a daemon thread connects to a deleted
    DB and prints a spurious traceback into a later check."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and manager._threads:
        time.sleep(0.01)


class MockThClawsHandler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        # /healthz drives the poll; any other path is the agent_info probe.
        payload = {"ok": WORKER_OK["value"]} if self.path == "/healthz" else {"name": "mock-thclaws"}
        body = json.dumps(payload).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        # /agent/run: emit some output but NO terminal [DONE] frame (worker disconnect).
        DISPATCH_COUNT["value"] += 1
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = b"event: text\ndata: partial output\n\n"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def check_poll_worker_health(db: Database) -> None:
    """poll_worker must rank a reachable-but-unhealthy worker ({"ok": false}) as offline,
    not online: the status keys off the worker's own ok flag, not mere reachability."""
    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockThClawsHandler)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    try:
        host, port = mock.server_address
        worker = db.upsert_worker({"base_url": f"http://{host}:{port}", "name": "mock"})
        manager = JobManager(db)

        WORKER_OK["value"] = True
        assert manager.poll_worker(worker["id"])["status"] == "online"

        WORKER_OK["value"] = False
        assert manager.poll_worker(worker["id"])["status"] == "offline"
    finally:
        mock.shutdown()
        mock.server_close()


def check_submit_routing_failure_no_orphan(db: Database) -> None:
    """submit() must not create a conversation when routing fails (no workers registered):
    a failed request leaves no orphan conversation behind."""
    manager = JobManager(db)
    before = len(db.list_conversations())
    try:
        manager.submit({"prompt": "hello"})
    except ValueError as exc:
        assert "No workers" in str(exc), exc
    else:
        raise AssertionError("submit must fail when no workers are registered")
    assert len(db.list_conversations()) == before, "routing failure must not orphan a conversation"


def check_truncated_stream_fails(db: Database) -> None:
    """A worker stream that ends without a terminal [DONE] frame (disconnect mid-output)
    must mark the job failed, not succeeded — a truncated result must never be handed off
    as complete."""
    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockThClawsHandler)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    try:
        host, port = mock.server_address
        worker = db.upsert_worker({"base_url": f"http://{host}:{port}", "name": "mock-stream"})
        manager = JobManager(db, request_timeout_seconds=5)
        job = manager.submit({"prompt": "hello", "worker_id": worker["id"]})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = db.get_job(job["id"])
            if current and current["state"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.02)
        final = db.get_job(job["id"])
        assert final["state"] == "failed", f"truncated stream must fail, got {final['state']}"
        assert "DONE" in (final.get("error") or ""), final.get("error")
        _await_threads_drained(manager)
    finally:
        mock.shutdown()
        mock.server_close()


def check_cancel_before_dispatch(db: Database) -> None:
    """A job cancelled while still queued must finish 'cancelled' WITHOUT ever opening the
    worker stream — cancellation is checked before the job goes 'running'."""
    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockThClawsHandler)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    try:
        host, port = mock.server_address
        worker = db.upsert_worker({"base_url": f"http://{host}:{port}", "name": "cancel-mock"})
        manager = JobManager(db, request_timeout_seconds=5)
        job = db.create_job({"worker_id": worker["id"], "prompt": "hi", "state": "queued"})
        db.mark_cancel_requested(job["id"])
        DISPATCH_COUNT["value"] = 0
        manager._run(job["id"])  # run synchronously, as the worker thread would
        final = db.get_job(job["id"])
        assert final["state"] == "cancelled", f"expected cancelled, got {final['state']}"
        assert DISPATCH_COUNT["value"] == 0, "cancelled-while-queued job must not dispatch to the worker"
    finally:
        mock.shutdown()
        mock.server_close()


def check_reconcile_jobs(db: Database) -> None:
    """After a restart, a job left 'running' in the DB (its thread gone) must be reconciled to
    a terminal 'failed' state, not wedged 'running' forever."""
    worker = db.upsert_worker({"base_url": "http://127.0.0.1:9", "name": "stale"})
    job = db.create_job({"worker_id": worker["id"], "prompt": "hi", "state": "queued"})
    db.update_job(job["id"], state="running", started_at="2026-01-01T00:00:00Z")
    JobManager(db).reconcile_jobs()
    final = db.get_job(job["id"])
    assert final["state"] == "failed", f"orphaned running job must reconcile to failed, got {final['state']}"
    assert final.get("finished_at"), "reconciled job must have finished_at set"


def check_upsert_requires_fields(db: Database) -> None:
    """Missing required upsert fields must raise ValueError (-> HTTP 400), not KeyError (500)."""
    for payload in ({}, {"name": "x"}):
        try:
            db.upsert_worker(payload)
        except ValueError:
            pass
        else:
            raise AssertionError("upsert_worker must reject a missing base_url with ValueError")
    worker = db.upsert_worker({"base_url": "http://127.0.0.1:10", "name": "w"})
    for payload in ({"worker_id": worker["id"]}, {"worker_id": worker["id"], "workspace_key": "k"}):
        try:
            db.upsert_workspace(payload)
        except ValueError:
            pass
        else:
            raise AssertionError("upsert_workspace must reject missing required fields with ValueError")


class CombinedFrameHandler(BaseHTTPRequestHandler):
    """Mock worker that emits ONE frame carrying both an `id` and `text`, then [DONE]."""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        payload = {"ok": True} if self.path == "/healthz" else {"name": "mock"}
        body = json.dumps(payload).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = b'data: {"id":"msg-1","text":"hello world"}\n\ndata: [DONE]\n\n'
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def check_combined_frame_text(db: Database) -> None:
    """A frame carrying BOTH an `id` and `text` must NOT have its text dropped (the `id` is not a
    session id, and there is no early `continue` skipping extract_text)."""
    mock = ThreadingHTTPServer(("127.0.0.1", 0), CombinedFrameHandler)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    try:
        host, port = mock.server_address
        worker = db.upsert_worker({"base_url": f"http://{host}:{port}", "name": "combined"})
        manager = JobManager(db, request_timeout_seconds=5)
        job = manager.submit({"prompt": "x", "worker_id": worker["id"]})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = db.get_job(job["id"])
            if current and current["state"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.02)
        final = db.get_job(job["id"])
        assert final["state"] == "succeeded", f"expected succeeded, got {final['state']}"
        assert (final.get("assistant_text") or "") == "hello world", f"combined-frame text dropped: {final.get('assistant_text')!r}"
        _await_threads_drained(manager)
        # This mock is also an old worker (no `usage` SSE event): the job succeeds and its
        # usage row records NULL token counts, never 0 or garbage (T1a).
        job_usage = next(e for e in db.list_usage_events() if e.get("job_id") == job["id"])
        assert job_usage["tokens_prompt"] is None and job_usage["tokens_output"] is None, job_usage
    finally:
        mock.shutdown()
        mock.server_close()


class MalformedUsageHandler(BaseHTTPRequestHandler):
    """Mock worker that emits only malformed `usage` frames (strings, negatives, bools,
    ints past SQLite's 64-bit range, non-dict, non-JSON) before valid text + [DONE]."""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = (
            b'event: usage\ndata: {"prompt_tokens": "many", "completion_tokens": -5}\n\n'
            b'event: usage\ndata: {"prompt_tokens": true, "cached_input_tokens": 1.5}\n\n'
            b'event: usage\ndata: {"prompt_tokens": 18446744073709551616, "completion_tokens": 9223372036854775808}\n\n'
            b'event: usage\ndata: ["not", "a", "dict"]\n\n'
            b"event: usage\ndata: not-json\n\n"
            b"event: text\ndata: ok\n\n"
            b"data: [DONE]\n\n"
        )
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def check_malformed_usage_tolerated(db: Database) -> None:
    """Malformed worker usage payloads (strings, negatives, bools, over-64-bit ints, non-dict,
    non-JSON) must be tolerated: the job succeeds and its usage ledger row EXISTS with NULL
    token counts — never a crash, never a bogus value, never a dropped row (an over-range int
    reaching SQLite raises OverflowError and would lose the whole usage event) (T1a)."""
    mock = ThreadingHTTPServer(("127.0.0.1", 0), MalformedUsageHandler)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    try:
        host, port = mock.server_address
        worker = db.upsert_worker({"base_url": f"http://{host}:{port}", "name": "bad-usage"})
        manager = JobManager(db, request_timeout_seconds=5)
        job = manager.submit({"prompt": "x", "worker_id": worker["id"]})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = db.get_job(job["id"])
            if current and current["state"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.02)
        final = db.get_job(job["id"])
        assert final["state"] == "succeeded", f"malformed usage must not fail the job, got {final['state']}"
        _await_threads_drained(manager)
        job_usage = next(e for e in db.list_usage_events() if e.get("job_id") == job["id"])
        assert job_usage["tokens_prompt"] is None and job_usage["tokens_output"] is None, job_usage
    finally:
        mock.shutdown()
        mock.server_close()


class DripHandler(BaseHTTPRequestHandler):
    """Mock worker that drips bytes WITHOUT a newline forever — readline() never returns, so
    only the stream watchdog (not a per-line check) can cut it."""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for _ in range(200):
                self.wfile.write(b"data: partial")  # no newline -> never a complete SSE line
                self.wfile.flush()
                time.sleep(0.1)
        except Exception:
            pass  # connection closed by the watchdog


def check_stream_deadline_bytedrip(db: Database) -> None:
    """A worker dripping bytes without a newline must be cut by the stream deadline (watchdog),
    not pin the thread until the 30s socket timeout."""
    mock = ThreadingHTTPServer(("127.0.0.1", 0), DripHandler)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    try:
        host, port = mock.server_address
        worker = db.upsert_worker({"base_url": f"http://{host}:{port}", "name": "drip"})
        manager = JobManager(db, request_timeout_seconds=5)
        manager.max_stream_seconds = 0.3
        job = manager.submit({"prompt": "x", "worker_id": worker["id"]})
        start = time.monotonic()
        while time.monotonic() < start + 8:
            current = db.get_job(job["id"])
            if current and current["state"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.02)
        elapsed = time.monotonic() - start
        final = db.get_job(job["id"])
        assert final["state"] == "failed", f"byte-drip must fail, got {final['state']}"
        assert elapsed < 4, f"deadline must cut the drip promptly (not the 30s socket timeout); took {elapsed:.2f}s"
        _await_threads_drained(manager)
    finally:
        mock.shutdown()
        mock.server_close()


class SlowHeaderStreamHandler(BaseHTTPRequestHandler):
    """Mock worker that drips the STATUS LINE / HEADER bytes and never completes the header
    block — so urlopen() blocks in the open phase, BEFORE iter_sse can start checking the
    deadline. Only the open-phase watchdog can cut this."""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def handle_one_request(self) -> None:
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if not self.raw_requestline:
                self.close_connection = True
                return
            self.wfile.write(b"HTTP/1.1 200 OK\r\n")
            for _ in range(200):  # ~20s of dribbling — outlasts the test's patience window
                self.wfile.write(b"X")
                self.wfile.flush()
                time.sleep(0.1)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # connection closed by the watchdog


def check_stream_deadline_header_drip(db: Database) -> None:
    """A worker dripping the RESPONSE HEADERS (not the body) must also be cut by the stream
    deadline — the open phase of run_agent_stream is bounded by stream_deadline. (Mutation:
    drop `deadline=stream_deadline` on run_agent_stream's _request → the open phase runs
    unbounded, the job never leaves 'running' within the window → red.)"""
    mock = ThreadingHTTPServer(("127.0.0.1", 0), SlowHeaderStreamHandler)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    try:
        host, port = mock.server_address
        worker = db.upsert_worker({"base_url": f"http://{host}:{port}", "name": "hdrdrip"})
        manager = JobManager(db, request_timeout_seconds=5)
        manager.max_stream_seconds = 0.3
        job = manager.submit({"prompt": "x", "worker_id": worker["id"]})
        start = time.monotonic()
        while time.monotonic() < start + 8:
            current = db.get_job(job["id"])
            if current and current["state"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.02)
        elapsed = time.monotonic() - start
        final = db.get_job(job["id"])
        assert final["state"] == "failed", f"header-drip must fail, got {final['state']}"
        assert elapsed < 4, f"open-phase deadline must cut the header drip promptly; took {elapsed:.2f}s"
        _await_threads_drained(manager)
    finally:
        mock.shutdown()
        mock.server_close()


# Planted ONLY in tool/skill input+output — the fields T2 projects away and must never persist.
STRUCTURED_MARKER = "planted-tool-payload-marker-a1b2c3d4e5f6"


class StructuredEventHandler(BaseHTTPRequestHandler):
    """Mock worker emitting a scripted structured-event sequence: assistant text, thinking,
    user_message_injected, a tool start/result pair, a denial, a skill invoke/result pair, an
    unknown future event, then [DONE]. The secret literal is planted only in tool/skill input
    and output — never in assistant text, thinking, or injected-message frames."""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        frames = [
            ("text", "hello world"),
            # Legacy OpenAI-compat bare-string text frames — still assistant text (back-compat).
            ("delta", "-delta"),
            ("content", "-content"),
            ("thinking", json.dumps({"delta": "internal reasoning, no secret"})),
            ("user_message_injected", json.dumps({"text": "re-injected user note"})),
            ("tool_use_start", json.dumps({"id": "t1", "name": "Bash", "input": {"command": f"echo {STRUCTURED_MARKER}"}})),
            # This result ALSO carries a session_id — a worker may tag every frame. It must STILL
            # be projected + stored (not dropped by the session branch) AND bind the session.
            ("tool_use_result", json.dumps({"id": "t1", "name": "Bash", "output": f"{STRUCTURED_MARKER} in stdout", "session_id": "sess-from-tool"})),
            ("tool_use_denied", json.dumps({"id": "t2", "name": "WebFetch"})),
            # A real failed result: the worker sends its own {"status":"error"} — Atlas must honor
            # it, not misclassify as ok from absent is_error/error keys.
            ("tool_use_result", json.dumps({"id": "t3", "name": "Grep", "status": "error", "output": "boom"})),
            ("skill_invoked", json.dumps({"id": "s1", "name": "pdf"})),
            ("skill_invoked_result", json.dumps({"id": "s1", "name": "pdf", "output": f"contains {STRUCTURED_MARKER}"})),
            ("brand_new_event", json.dumps({"note": "worker from the future"})),
        ]
        body = "".join(f"event: {name}\ndata: {data}\n\n" for name, data in frames) + "data: [DONE]\n\n"
        raw = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def check_structured_events(db: Database) -> None:
    """T2 structured-event surfaces:
    (1) Parser scoping — `thinking` and `user_message_injected` are NOT folded into
        assistant_text; each is stored as its own job_events row; plain text still accumulates.
    (2) Tool/skill payloads are projected to structural metadata only — a secret planted in tool
        input AND output finds ZERO occurrences in a byte-scan of the SQLite file.
    (3) Statuses derive from event type; unknown event names are stored without crashing."""
    mock = ThreadingHTTPServer(("127.0.0.1", 0), StructuredEventHandler)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    try:
        host, port = mock.server_address
        worker = db.upsert_worker({"base_url": f"http://{host}:{port}", "name": "structured"})
        manager = JobManager(db, request_timeout_seconds=5)
        job = manager.submit({"prompt": "x", "worker_id": worker["id"]})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = db.get_job(job["id"])
            if current and current["state"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.02)
        final = db.get_job(job["id"])
        assert final["state"] == "succeeded", f"structured stream must succeed, got {final['state']}"
        _await_threads_drained(manager)

        # (1) Parser scoping: assistant text (incl. legacy delta/content frames) accumulates;
        # thinking/user_message_injected do NOT fold in (they use distinct event names).
        assert (final.get("assistant_text") or "") == "hello world-delta-content", repr(final.get("assistant_text"))
        events = db.get_job_events_after(job["id"], 0, limit=1000)
        by_type: dict[str, list[dict]] = {}
        for event in events:
            by_type.setdefault(event["event_type"], []).append(event["payload"])
        assert "thinking" in by_type and "user_message_injected" in by_type, sorted(by_type)
        assert by_type["thinking"][0].get("delta"), by_type["thinking"][0]

        # (2) Secret planted in tool input AND output must not survive anywhere in the DB file.
        assert STRUCTURED_MARKER.encode("utf-8") not in db.path.read_bytes(), "tool payload secret leaked into the DB file"

        # (3) Tool/skill rows carry ONLY structural metadata; statuses derive from the event type.
        start = by_type["tool_use_start"][0]
        assert "input" not in start and "output" not in start, start
        assert start["name"] == "Bash" and start["status"] == "started", start
        assert start["input_bytes"] > 0 and len(start["input_sha256"]) == 64, start
        result = by_type["tool_use_result"][0]
        assert "output" not in result and result["status"] == "ok", result
        assert result["output_bytes"] > 0 and len(result["output_sha256"]) == 64, result
        # The session-tagged tool result was still stored (not dropped) AND bound the session.
        assert "session_id" not in result, result  # projection strips everything but structural keys
        assert "session" in by_type and db.get_job(job["id"]).get("thclaws_session_id") == "sess-from-tool"
        assert by_type["tool_use_denied"][0]["status"] == "denied", by_type["tool_use_denied"][0]
        # The worker-reported {"status":"error"} result is honored, not derived to "ok".
        assert any(r["status"] == "error" and r["name"] == "Grep" for r in by_type["tool_use_result"]), by_type["tool_use_result"]
        assert by_type["skill_invoked"][0]["status"] == "started", by_type["skill_invoked"][0]
        assert by_type["skill_invoked_result"][0]["status"] == "ok", by_type["skill_invoked_result"][0]
        assert "brand_new_event" in by_type, sorted(by_type)  # unknown event stored, no crash
    finally:
        mock.shutdown()
        mock.server_close()


class BigToolPayloadHandler(BaseHTTPRequestHandler):
    """Emits large `tool_use_result` frames whose big `output` is PROJECTED AWAY before storage,
    then [DONE]. The output cap must count the RAW frame bytes the worker pushed (which Atlas
    reads and hashes), not the tiny projected record — otherwise a worker hides volume in
    projected-away tool payloads and evades ATLAS_MAX_JOB_OUTPUT_BYTES."""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        big = "z" * 4096  # projected to a ~120-byte {id,name,status,output_bytes,output_sha256} record
        frames = "".join(
            f'event: tool_use_result\ndata: {json.dumps({"id": f"t{i}", "name": "Bash", "output": big})}\n\n'
            for i in range(6)
        )
        raw = (frames + "data: [DONE]\n\n").encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def check_structured_output_bounded(db: Database) -> None:
    """Structured frames leave the assistant-text path, so the output cap must count the RAW
    bytes the worker pushed — including large tool input/output that projection shrinks to a
    tiny record. With a small cap and big (projected-away) tool payloads the job must FAIL with
    the output-exceeded error before [DONE]; counting only the projected record would let it
    succeed (the evasion this locks)."""
    mock = ThreadingHTTPServer(("127.0.0.1", 0), BigToolPayloadHandler)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    try:
        host, port = mock.server_address
        worker = db.upsert_worker({"base_url": f"http://{host}:{port}", "name": "big-tool"})
        manager = JobManager(db, request_timeout_seconds=5)
        manager.max_output_bytes = 2000  # < one raw tool frame → cap trips; projected record (~120B) would not
        job = manager.submit({"prompt": "x", "worker_id": worker["id"]})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = db.get_job(job["id"])
            if current and current["state"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.02)
        final = db.get_job(job["id"])
        assert final["state"] == "failed", f"structured-frame flood must fail, got {final['state']}"
        assert "exceeded" in (final.get("error") or ""), final.get("error")
        _await_threads_drained(manager)
    finally:
        mock.shutdown()
        mock.server_close()


class BigEventNameHandler(BaseHTTPRequestHandler):
    """Emits frames with a huge `event:` NAME and tiny data, then [DONE]. The output cap must
    count the event-name bytes too, or a worker hides volume in the frame name (not `data`)."""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        big_name = "x" * 4096
        frames = "".join(f"event: {big_name}\ndata: {{}}\n\n" for _ in range(6))
        raw = (frames + "data: [DONE]\n\n").encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def check_event_name_bytes_bounded(db: Database) -> None:
    """The output cap counts event NAME + data. A worker streaming frames with huge event names
    (tiny data) must still trip the cap and FAIL — counting only `data` would let it succeed."""
    mock = ThreadingHTTPServer(("127.0.0.1", 0), BigEventNameHandler)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    try:
        host, port = mock.server_address
        worker = db.upsert_worker({"base_url": f"http://{host}:{port}", "name": "big-name"})
        manager = JobManager(db, request_timeout_seconds=5)
        manager.max_output_bytes = 2000  # < one 4 KiB event name → cap trips; data alone ({}) would not
        job = manager.submit({"prompt": "x", "worker_id": worker["id"]})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = db.get_job(job["id"])
            if current and current["state"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.02)
        final = db.get_job(job["id"])
        assert final["state"] == "failed", f"huge event-name flood must fail, got {final['state']}"
        assert "exceeded" in (final.get("error") or ""), final.get("error")
        _await_threads_drained(manager)
    finally:
        mock.shutdown()
        mock.server_close()


class BigTerminalHandler(BaseHTTPRequestHandler):
    """Emits a SINGLE terminal frame with a huge `event:` name and `data: [DONE]`. The output
    cap must count the terminal frame too, or a worker pads the [DONE] frame to bypass it."""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        raw = (f"event: {'x' * 4096}\ndata: [DONE]\n\n").encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def check_terminal_frame_bytes_bounded(db: Database) -> None:
    """A padded terminal frame (huge event name + `data: [DONE]`) must be counted BEFORE the
    [DONE] branch, so it trips the cap and FAILS instead of being persisted uncounted as a
    'done' row. Counting after the [DONE] check would let it succeed (the bypass this locks)."""
    mock = ThreadingHTTPServer(("127.0.0.1", 0), BigTerminalHandler)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    try:
        host, port = mock.server_address
        worker = db.upsert_worker({"base_url": f"http://{host}:{port}", "name": "big-terminal"})
        manager = JobManager(db, request_timeout_seconds=5)
        manager.max_output_bytes = 2000  # < the 4 KiB terminal event name
        job = manager.submit({"prompt": "x", "worker_id": worker["id"]})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = db.get_job(job["id"])
            if current and current["state"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.02)
        final = db.get_job(job["id"])
        assert final["state"] == "failed", f"padded terminal frame must fail, got {final['state']}"
        assert "exceeded" in (final.get("error") or ""), final.get("error")
        _await_threads_drained(manager)
    finally:
        mock.shutdown()
        mock.server_close()


class PaddedDataHandler(BaseHTTPRequestHandler):
    """Emits `data:` lines padded with thousands of LEADING spaces (which iter_sse lstrips away)
    around a tiny `{}`, then [DONE]. The normalized data is 2 bytes but the wire frame is huge —
    the cap must count raw wire bytes, or whitespace padding bypasses it."""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        pad = " " * 4096
        frames = "".join(f"data:{pad}{{}}\n\n" for _ in range(6))  # lstrip() → data == "{}"
        raw = (frames + "data: [DONE]\n\n").encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def check_padded_frame_bytes_bounded(db: Database) -> None:
    """A worker padding `data:` lines with whitespace (stripped to a tiny payload) must still trip
    the output cap and FAIL — the cap counts raw WIRE bytes (iter_sse.raw_bytes), not the
    normalized fields. Counting normalized data would let this succeed (the bypass this locks)."""
    mock = ThreadingHTTPServer(("127.0.0.1", 0), PaddedDataHandler)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    try:
        host, port = mock.server_address
        worker = db.upsert_worker({"base_url": f"http://{host}:{port}", "name": "padded"})
        manager = JobManager(db, request_timeout_seconds=5)
        manager.max_output_bytes = 2000  # < one 4 KiB padded wire frame; normalized "{}" (2 B) would not
        job = manager.submit({"prompt": "x", "worker_id": worker["id"]})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = db.get_job(job["id"])
            if current and current["state"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.02)
        final = db.get_job(job["id"])
        assert final["state"] == "failed", f"whitespace-padded flood must fail, got {final['state']}"
        assert "exceeded" in (final.get("error") or ""), final.get("error")
        _await_threads_drained(manager)
    finally:
        mock.shutdown()
        mock.server_close()


class CommentFloodHandler(BaseHTTPRequestHandler):
    """Emits large SSE comment/heartbeat lines (`: …`) — which yield NO event — then [DONE].
    A per-yielded-event byte count would miss these entirely; the cap must count raw wire bytes
    at the source (iter_sse) so data-less frames can't push traffic past the output cap."""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        frames = "".join(f": {'k' * 4096}\n\n" for _ in range(6))  # comment lines: no event yielded
        raw = (frames + "data: [DONE]\n\n").encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def check_comment_flood_bounded(db: Database) -> None:
    """Data-less comment/heartbeat frames yield no event, so a per-event byte count never sees
    them. The cap counts raw wire bytes in iter_sse, so a comment flood must still trip it and
    FAIL before reaching [DONE] — otherwise a worker streams unbounded control lines past the cap."""
    mock = ThreadingHTTPServer(("127.0.0.1", 0), CommentFloodHandler)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    try:
        host, port = mock.server_address
        worker = db.upsert_worker({"base_url": f"http://{host}:{port}", "name": "comment-flood"})
        manager = JobManager(db, request_timeout_seconds=5)
        manager.max_output_bytes = 2000  # < the cumulative comment bytes
        job = manager.submit({"prompt": "x", "worker_id": worker["id"]})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = db.get_job(job["id"])
            if current and current["state"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.02)
        final = db.get_job(job["id"])
        assert final["state"] == "failed", f"comment flood must fail, got {final['state']}"
        assert "exceeded" in (final.get("error") or ""), final.get("error")
        _await_threads_drained(manager)
    finally:
        mock.shutdown()
        mock.server_close()


LEGACY_TOOL_MARKER = "planted-legacy-payload-marker-1a2b3c4d"


def check_legacy_tool_payload_redacted_on_read(tmp: Path) -> None:
    """A DB written before T2's projection can hold raw tool `input`/`output` in job_events. The
    SSE read path must redact those on read so no raw payload (and no planted secret) ever
    reaches a client, even for old jobs — byte-scan the streamed response for zero occurrences."""
    runtime = AtlasRuntime(
        Config(
            host="127.0.0.1", port=0, db_path=tmp / "legacy.sqlite", api_token=None,
            request_timeout_seconds=2, enable_loopback_without_token=False,
            secret_key="legacy-secret", upload_dir=tmp / "legacy-uploads",
        )
    )
    user = runtime.db.create_user("admin", "admin-pw", "admin")
    _, token = runtime.db.create_api_token(user["id"], "legacy read check")
    worker = runtime.db.upsert_worker({"base_url": "http://127.0.0.1:9", "name": "legacy"})
    job = runtime.db.create_job({"worker_id": worker["id"], "prompt": "x", "state": "queued"})
    # Simulate the pre-T2 append path: tool rows stored with their RAW payloads. Two shapes —
    # input/output, and an error-only result (a raw field OTHER than input/output).
    runtime.db.append_job_event(
        job["id"], "tool_use_result",
        {"id": "t1", "name": "Bash", "input": {"cmd": LEGACY_TOOL_MARKER}, "output": f"{LEGACY_TOOL_MARKER} in stdout", "event": "tool_use_result"},
    )
    runtime.db.append_job_event(
        job["id"], "tool_use_result",
        {"id": "t2", "name": "WebFetch", "error": f"{LEGACY_TOOL_MARKER} boom"},
    )
    runtime.db.update_job(job["id"], state="succeeded", finished_at=now_iso())

    server = AtlasHttpServer(("127.0.0.1", 0), runtime)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/api/jobs/{job['id']}/events?token={token}"
        body = urllib.request.urlopen(url, timeout=5).read().decode("utf-8")  # nosec B310 - loopback test server
        assert LEGACY_TOOL_MARKER not in body, "legacy raw tool payload leaked over the events stream"
        for raw_key in ('"input":', '"output":', '"error":'):
            assert raw_key not in body, f"raw legacy field {raw_key} streamed: {body}"
        # Both events still surface — as structural metadata (name + derived status), not payload.
        assert '"name":"Bash"' in body and '"status":"ok"' in body, body
        assert '"name":"WebFetch"' in body and '"status":"error"' in body, body  # error-only → status error
    finally:
        runtime.close()  # stop the reaper daemon before the tempdir exits
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def check_reconcile_cancelled(db: Database) -> None:
    """A job cancelled but not yet observed by its thread before a restart must reconcile to
    'cancelled', not 'failed'."""
    worker = db.upsert_worker({"base_url": "http://127.0.0.1:9", "name": "x"})
    job = db.create_job({"worker_id": worker["id"], "prompt": "hi", "state": "queued"})
    db.update_job(job["id"], state="running", cancel_requested=1, started_at="2026-01-01T00:00:00Z")
    JobManager(db).reconcile_jobs()
    final = db.get_job(job["id"])
    assert final["state"] == "cancelled", f"cancelled-but-unobserved job must reconcile to cancelled, got {final['state']}"


def main() -> None:
    with TemporaryDirectory() as tmp:
        check_combined_frame_text(Database(Path(tmp) / "combined.sqlite"))
        check_malformed_usage_tolerated(Database(Path(tmp) / "badusage.sqlite"))
        check_structured_events(Database(Path(tmp) / "structured.sqlite"))
        check_structured_output_bounded(Database(Path(tmp) / "bigtool.sqlite"))
        check_event_name_bytes_bounded(Database(Path(tmp) / "bigname.sqlite"))
        check_terminal_frame_bytes_bounded(Database(Path(tmp) / "bigterm.sqlite"))
        check_padded_frame_bytes_bounded(Database(Path(tmp) / "padded.sqlite"))
        check_comment_flood_bounded(Database(Path(tmp) / "comment.sqlite"))
        check_legacy_tool_payload_redacted_on_read(Path(tmp))
        check_stream_deadline_bytedrip(Database(Path(tmp) / "drip.sqlite"))
        check_stream_deadline_header_drip(Database(Path(tmp) / "hdrdrip.sqlite"))
        check_reconcile_cancelled(Database(Path(tmp) / "reconcancel.sqlite"))
        check_submit_routing_failure_no_orphan(Database(Path(tmp) / "orphan.sqlite"))
        check_poll_worker_health(Database(Path(tmp) / "health.sqlite"))
        check_truncated_stream_fails(Database(Path(tmp) / "stream.sqlite"))
        check_cancel_before_dispatch(Database(Path(tmp) / "cancel.sqlite"))
        check_reconcile_jobs(Database(Path(tmp) / "reconcile.sqlite"))
        check_upsert_requires_fields(Database(Path(tmp) / "upsert.sqlite"))
    print("jobs check ok")


if __name__ == "__main__":
    main()
