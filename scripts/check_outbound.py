"""OB-1 hermetic check (docs/plans/input-adapter-return-path-plan.md).

Verifies the signed outbound delivery return path: a run completing with `_meta.reply.webhook`
delivers a signed POST to the callback (correct run_id/state/correlation_id/artifacts); a
non-allowlisted/private callback is `blocked` and never sent; a receiver that keeps failing is
retried up to `ATLAS_OUTBOUND_MAX_ATTEMPTS` then dead-lettered as `failed` WITHOUT touching the
run's own outcome, and the same `delivery_id` is reused across every attempt (dedupable);
`POST /api/deliveries/{id}/retry` re-attempts a `failed` delivery within the bound; a missing
`ATLAS_SECRET_KEY` refuses to send unsigned and records why; and attempts on one delivery row
are exclusively owned — a reconcile or manual retry racing a live in-flight attempt is skipped
or refused instead of double-sending and regressing `delivered` to `failed`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.app import AtlasHttpServer, AtlasRuntime
from atlas.config import Config
from atlas.db import now_iso
from atlas.outbound import _completion_delivery_id


class MockThClawsHandler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:
        assert self.path == "/agent/run"
        self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
        body = b"event: text\ndata: delivered\n\nevent: done\ndata: [DONE]\n\n"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class MockReceiverHandler(BaseHTTPRequestHandler):
    received: list[dict] = []
    # path -> remaining number of times to answer 500 before answering 200.
    fail_counts: dict[str, int] = {}
    # /reply/race: the FIRST request parks here (signalling race_first_seen) until the test
    # sets race_release, then gets 200; any request that arrives while the first is parked
    # gets an instant 500 — a losing concurrent sender that must never be allowed to run.
    race_first_seen = threading.Event()
    race_release = threading.Event()

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length)
        MockReceiverHandler.received.append(
            {"path": self.path, "signature": self.headers.get("X-Atlas-Signature"), "raw": raw, "body": json.loads(raw or b"{}")}
        )
        if self.path == "/reply/slow":
            # 200 immediately, then drip the body far slower than the client's attempt timeout.
            # The client must give up reading the body (but has already captured the status)
            # well before this loop finishes.
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            try:
                for _ in range(50):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.06)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # the client closed once its drain deadline passed — expected
            return
        if self.path == "/reply/slowheader":
            # Trickle the response one byte at a time — starting with the status line itself —
            # far slower than the timeout, so getresponse() can't even parse a status within the
            # deadline. Each byte resets the per-recv socket timeout, so only the total-deadline
            # watchdog in _send bounds this; without it the attempt would run for the full ~5s.
            try:
                for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n":
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(0.15)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # the client's watchdog shut the socket down at the deadline — expected
            return
        if self.path == "/reply/race":
            if not MockReceiverHandler.race_first_seen.is_set():
                MockReceiverHandler.race_first_seen.set()
                assert MockReceiverHandler.race_release.wait(timeout=10), "race test never released the receiver"
                self.send_response(HTTPStatus.OK)
            else:
                self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        remaining = MockReceiverHandler.fail_counts.get(self.path, 0)
        if remaining > 0:
            MockReceiverHandler.fail_counts[self.path] = remaining - 1
            self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Length", "0")
        self.end_headers()


SECRET_KEY = "outbound-signing-secret"
MAX_ATTEMPTS = 3


def main() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        mock_worker = ThreadingHTTPServer(("127.0.0.1", 0), MockThClawsHandler)
        mock_worker_thread = threading.Thread(target=mock_worker.serve_forever, daemon=True)
        mock_worker_thread.start()

        mock_receiver = ThreadingHTTPServer(("127.0.0.1", 0), MockReceiverHandler)
        mock_receiver_thread = threading.Thread(target=mock_receiver.serve_forever, daemon=True)
        mock_receiver_thread.start()
        receiver_base = f"http://127.0.0.1:{mock_receiver.server_address[1]}"

        runtime = AtlasRuntime(
            Config(
                host="127.0.0.1",
                port=0,
                db_path=root / "atlas.sqlite",
                api_token=None,
                request_timeout_seconds=2,
                enable_loopback_without_token=True,
                upload_dir=root / "uploads",
                secret_key=SECRET_KEY,
                outbound_allowlist=("127.0.0.1",),
                outbound_max_attempts=MAX_ATTEMPTS,
                outbound_timeout_seconds=2,
            )
        )
        worker = runtime.db.upsert_worker(
            {"name": "Mock OB worker", "base_url": f"http://127.0.0.1:{mock_worker.server_address[1]}"}
        )
        definition = runtime.db.create_workflow_definition(
            {
                "name": "Deliverable workflow",
                "graph": {
                    "start": "work",
                    "nodes": [{"id": "work", "type": "worker", "worker_id": worker["id"], "prompt": "go", "outputs": ["notes"]}],
                    "edges": [],
                },
                "policy": {"max_jobs": 1},
            }
        )

        server = AtlasHttpServer(("127.0.0.1", 0), runtime)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            # A. Happy path: run completes -> one signed POST, correct fields + artifacts.
            run_a = start_run(base_url, definition["id"], f"{receiver_base}/reply/ok", "corr-ok")
            wait_for_run(runtime, run_a)
            delivery_a = wait_for_delivery(runtime, run_a, "delivered")
            posts_ok = [item for item in MockReceiverHandler.received if item["path"] == "/reply/ok"]
            assert len(posts_ok) == 1, posts_ok
            body = posts_ok[0]["body"]
            assert body["delivery_id"] == delivery_a["id"]
            assert body["run_id"] == run_a
            assert body["state"] == "succeeded"
            assert body["correlation_id"] == "corr-ok"
            assert body["artifacts"] == [{"key": "notes", "kind": "text", "content": "delivered"}], body["artifacts"]
            expected_sig = "sha256=" + hmac.new(SECRET_KEY.encode(), posts_ok[0]["raw"], hashlib.sha256).hexdigest()
            assert hmac.compare_digest(posts_ok[0]["signature"], expected_sig)
            wait_for_audit(runtime, "delivery.delivered", delivery_a["id"])

            # B. A private-IP literal that is not in ATLAS_OUTBOUND_ALLOWLIST is blocked, never
            #    sent (tested against the delivery mechanism directly: IA-1 would already refuse
            #    a run promising this callback_url at ingress, so drive OB-1's own guard the way
            #    a stale/edited allowlist or a manual retry would encounter it).
            blocked_seed = runtime.db.create_delivery(
                {"run_id": run_a, "url": "https://10.1.2.3/reply", "correlation_id": "corr-blocked", "max_attempts": MAX_ATTEMPTS}
            )
            status, retried = request_json(base_url, "POST", f"/api/deliveries/{blocked_seed['id']}/retry")
            assert status == 202, retried
            assert retried["delivery"]["status"] == "blocked", retried
            assert "ATLAS_OUTBOUND_ALLOWLIST" in retried["delivery"]["last_error"], retried
            wait_for_audit(runtime, "delivery.blocked", blocked_seed["id"])

            # C. Receiver fails MAX_ATTEMPTS times -> failed (dead-letter); run stays succeeded;
            #    every attempt reuses the SAME delivery_id (receiver-dedupable). A manual retry
            #    (relay now fixed) then succeeds -> delivered.
            MockReceiverHandler.fail_counts["/reply/flaky"] = MAX_ATTEMPTS
            run_c = start_run(base_url, definition["id"], f"{receiver_base}/reply/flaky", "corr-flaky")
            run_c_row = wait_for_run(runtime, run_c)
            delivery_c = wait_for_delivery(runtime, run_c, "failed")
            assert delivery_c["attempts"] == MAX_ATTEMPTS, delivery_c
            assert run_c_row["state"] == "succeeded", run_c_row
            flaky_posts = [item for item in MockReceiverHandler.received if item["path"] == "/reply/flaky"]
            assert len(flaky_posts) == MAX_ATTEMPTS
            assert len({post["body"]["delivery_id"] for post in flaky_posts}) == 1
            wait_for_audit(runtime, "delivery.failed", delivery_c["id"])

            status, retried_c = request_json(base_url, "POST", f"/api/deliveries/{delivery_c['id']}/retry")
            assert status == 202, retried_c
            assert retried_c["delivery"]["status"] == "delivered", retried_c
            flaky_posts_after = [item for item in MockReceiverHandler.received if item["path"] == "/reply/flaky"]
            assert len(flaky_posts_after) == MAX_ATTEMPTS + 1
            assert len({post["body"]["delivery_id"] for post in flaky_posts_after}) == 1

            # D. Missing ATLAS_SECRET_KEY refuses to send (never unsigned), recorded with reason.
            original_settings = runtime.outbound.settings
            runtime.outbound.settings = replace(original_settings, secret_key=None)
            try:
                run_d = start_run(base_url, definition["id"], f"{receiver_base}/reply/ok", "corr-nosecret")
                wait_for_run(runtime, run_d)
                delivery_d = wait_for_delivery(runtime, run_d, "blocked")
                assert "ATLAS_SECRET_KEY" in delivery_d["last_error"], delivery_d
            finally:
                runtime.outbound.settings = original_settings
            posts_ok_after = [item for item in MockReceiverHandler.received if item["path"] == "/reply/ok"]
            assert len(posts_ok_after) == 1, "no-secret run must never reach the receiver"

            # E. A callback_url carrying data where a secret would hide (userinfo, query string,
            #    or fragment) is blocked at send time too — defense in depth for a delivery row
            #    seeded directly (bypassing IA-1's ingress rejection of the same URL shapes). The
            #    guard is structural, so a param name a keyword denylist would miss is caught too.
            userinfo_seed = runtime.db.create_delivery(
                {"run_id": run_a, "url": f"http://user:pass@127.0.0.1:{mock_receiver.server_address[1]}/reply/ok"}
            )
            status, userinfo_retried = request_json(base_url, "POST", f"/api/deliveries/{userinfo_seed['id']}/retry")
            assert status == 202 and userinfo_retried["delivery"]["status"] == "blocked", userinfo_retried
            assert "credentials" in userinfo_retried["delivery"]["last_error"], userinfo_retried

            query_secret_seed = runtime.db.create_delivery(
                {"run_id": run_a, "url": f"{receiver_base}/reply/ok?webhook_secret=s3cr3t"}
            )
            status, query_retried = request_json(base_url, "POST", f"/api/deliveries/{query_secret_seed['id']}/retry")
            assert status == 202 and query_retried["delivery"]["status"] == "blocked", query_retried
            assert "query string" in query_retried["delivery"]["last_error"], query_retried
            assert len([item for item in MockReceiverHandler.received if item["path"] == "/reply/ok"]) == 1, (
                "a credential-leaking callback_url must never actually be sent"
            )

            # F. Restart recovery: OutboundService.reconcile() is the exact method
            #    AtlasRuntime.__init__ runs in a background thread at startup. (1) a delivery
            #    left `pending` (its attempt thread died with the old process) is resumed; (2) a
            #    completed run that asked for webhook delivery but crashed before a delivery row
            #    was ever written gets one created and attempted now.
            stuck_run = start_run(base_url, definition["id"], f"{receiver_base}/reply/stuck", "corr-stuck")
            wait_for_run(runtime, stuck_run)
            stuck_delivery = wait_for_delivery(runtime, stuck_run, "delivered")
            runtime.db.update_delivery(stuck_delivery["id"], status="pending", attempts=0, delivered_at=None)

            missing_run = runtime.db.create_workflow_run(
                {
                    "workflow_definition_id": definition["id"],
                    "name": "Crashed-before-delivery-row run",
                    "state": "succeeded",
                    "input": {"_meta": {"reply": {"mode": "webhook", "callback_url": f"{receiver_base}/reply/missing"}}},
                    "started_at": now_iso(),
                    "finished_at": now_iso(),
                }
            )
            assert not runtime.db.list_deliveries(run_id=missing_run["id"])

            runtime.outbound.reconcile()
            assert wait_for_delivery(runtime, stuck_run, "delivered")["id"] == stuck_delivery["id"]
            wait_for_delivery(runtime, missing_run["id"], "delivered")

            # F2. The completion delivery id is DETERMINISTIC per run, so the live completion path
            #     and a restart reconcile converge on ONE row (atomic INSERT OR IGNORE claim) —
            #     the receiver never gets two competing delivery_ids for the same run. Re-running
            #     reconcile and re-invoking the completion path add no duplicate rows.
            assert stuck_delivery["id"] == _completion_delivery_id(stuck_run), stuck_delivery
            missing_delivery = runtime.db.list_deliveries(run_id=missing_run["id"])
            assert len(missing_delivery) == 1 and missing_delivery[0]["id"] == _completion_delivery_id(missing_run["id"])
            runtime.outbound.reconcile()
            runtime.outbound.deliver_run_completion(runtime.db.get_workflow_run(stuck_run))
            assert {d["id"] for d in runtime.db.list_deliveries(run_id=stuck_run)} == {stuck_delivery["id"]}
            assert {d["id"] for d in runtime.db.list_deliveries(run_id=missing_run["id"])} == {missing_delivery[0]["id"]}

            # G. A receiver that answers 200 immediately but drips the body slowly must not hold
            #    a delivery open beyond its timeout (the socket timeout alone would not catch
            #    this — every individual read still completes inside it).
            slow_start = time.monotonic()
            run_g = start_run(base_url, definition["id"], f"{receiver_base}/reply/slow", "corr-slow")
            wait_for_run(runtime, run_g)
            delivery_g = wait_for_delivery(runtime, run_g, "delivered", timeout=6)
            elapsed = time.monotonic() - slow_start
            assert delivery_g["attempts"] == 1, delivery_g
            assert elapsed < 2.9, f"delivery took {elapsed:.2f}s — response drain did not respect the deadline"

            # H. A receiver that trickles the STATUS LINE / HEADERS (not just the body) slower
            #    than the timeout must still be bounded: the per-recv socket timeout resets on
            #    every byte, so only the total wall-clock watchdog in _send catches this. One
            #    bounded attempt (max_attempts=1) → failed, well within a small multiple of the
            #    2s timeout, and never delivered.
            slowheader_seed = runtime.db.create_delivery(
                {"run_id": run_a, "url": f"{receiver_base}/reply/slowheader", "max_attempts": 1}
            )
            header_start = time.monotonic()
            status, sh_retried = request_json(base_url, "POST", f"/api/deliveries/{slowheader_seed['id']}/retry")
            header_elapsed = time.monotonic() - header_start
            assert status == 202 and sh_retried["delivery"]["status"] == "failed", sh_retried
            assert header_elapsed < 2.9, f"slow-header attempt not bounded: {header_elapsed:.2f}s"

            # I. Attempt ownership: while a LIVE completion attempt is in flight (the receiver is
            #    holding its request open), a concurrent reconcile() must NOT drive the same
            #    pending row — its racing 500 would overwrite the live attempt's `delivered`
            #    (delivered -> failed regression), and a manual retry must be refused for the
            #    same reason. NOTE: everything between race_first_seen and race_release must stay
            #    well under outbound_timeout_seconds (2s) or the parked live attempt gets cut by
            #    _send's total deadline — reconcile skipping an owned row is immediate, so it does.
            run_i = start_run(base_url, definition["id"], f"{receiver_base}/reply/race", "corr-race")
            assert MockReceiverHandler.race_first_seen.wait(timeout=5), "live attempt never reached the receiver"
            # The live sender is now parked inside the receiver; its delivery row is still `pending`.
            reconcile_racer = threading.Thread(target=runtime.outbound.reconcile)
            reconcile_racer.start()
            reconcile_racer.join(timeout=5)
            assert not reconcile_racer.is_alive(), "reconcile did not return while the delivery was owned"
            status, retry_racer = request_json(base_url, "POST", f"/api/deliveries/{_completion_delivery_id(run_i)}/retry")
            assert status == 400 and "in progress" in retry_racer["error"], retry_racer
            MockReceiverHandler.race_release.set()
            delivery_i = wait_for_delivery(runtime, run_i, "delivered")
            assert delivery_i["attempts"] == 1, delivery_i
            race_posts = [item for item in MockReceiverHandler.received if item["path"] == "/reply/race"]
            assert len(race_posts) == 1, f"same delivery sent {len(race_posts)} times — attempt ownership not exclusive"
            # A late reconcile over the now-terminal row re-reads after claiming and leaves it alone.
            runtime.outbound.reconcile()
            delivery_i_after = runtime.db.get_delivery(delivery_i["id"])
            assert delivery_i_after and delivery_i_after["status"] == "delivered", delivery_i_after

            # J. Manual deliver_run builds its OWN fresh (random-id) delivery row and drives it
            #    directly; it must hold the attempt claim for that row so a restart reconcile —
            #    which scans ALL pending rows, not just deterministic completion ids — can't grab
            #    it mid-flight and double-send / regress delivered->failed. Poll-mode reply so the
            #    completion path never auto-delivers: the manual call is the only sender.
            MockReceiverHandler.race_first_seen.clear()
            MockReceiverHandler.race_release.clear()
            race_before = len([item for item in MockReceiverHandler.received if item["path"] == "/reply/race"])
            created_j = runtime.db.create_workflow_run(
                {
                    "workflow_definition_id": definition["id"],
                    "name": "Manual-deliver poll run",
                    "state": "succeeded",
                    "input": {"_meta": {"reply": {"mode": "poll", "callback_url": f"{receiver_base}/reply/race", "correlation_id": "corr-manual"}}},
                    "started_at": now_iso(),
                    "finished_at": now_iso(),
                }
            )
            run_j = runtime.db.get_workflow_run(created_j["id"])
            assert not runtime.db.list_deliveries(run_id=run_j["id"]), "poll-mode run must not auto-deliver"
            deliver_thread = threading.Thread(target=runtime.outbound.deliver_run, args=(run_j,))
            deliver_thread.start()
            assert MockReceiverHandler.race_first_seen.wait(timeout=5), "manual deliver never reached the receiver"
            # The manual row is pending AND claimed by deliver_run; a racing reconcile must skip it.
            reconcile_j = threading.Thread(target=runtime.outbound.reconcile)
            reconcile_j.start()
            reconcile_j.join(timeout=5)
            assert not reconcile_j.is_alive(), "reconcile did not return while the manual delivery was owned"
            MockReceiverHandler.race_release.set()
            deliver_thread.join(timeout=5)
            manual_rows = runtime.db.list_deliveries(run_id=run_j["id"])
            assert len(manual_rows) == 1 and manual_rows[0]["status"] == "delivered", manual_rows
            assert manual_rows[0]["attempts"] == 1, manual_rows
            race_after = len([item for item in MockReceiverHandler.received if item["path"] == "/reply/race"])
            assert race_after - race_before == 1, "manual delivery double-sent under a reconcile race"

            # GET /api/deliveries lists everything created above, filterable by run_id/status.
            # (run_a also carries the synthetic blocked_seed/userinfo_seed/query_secret_seed/
            # slowheader_seed deliveries from scenarios B, E, and H.)
            status, listed = request_json(base_url, "GET", f"/api/deliveries?run_id={run_a}")
            assert status == 200 and {d["id"] for d in listed["deliveries"]} == {
                delivery_a["id"], blocked_seed["id"], userinfo_seed["id"], query_secret_seed["id"], slowheader_seed["id"]
            }, listed
            status, failed_listed = request_json(base_url, "GET", "/api/deliveries?status=blocked")
            assert status == 200 and {d["id"] for d in failed_listed["deliveries"]} >= {blocked_seed["id"], delivery_d["id"]}
        finally:
            runtime.close()  # stop the reaper daemon before the tempdir exits
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)
            mock_worker.shutdown()
            mock_worker.server_close()
            mock_worker_thread.join(timeout=2)
            mock_receiver.shutdown()
            mock_receiver.server_close()
            mock_receiver_thread.join(timeout=2)

    print("outbound delivery check ok")


def start_run(base_url: str, definition_id: str, callback_url: str, correlation_id: str) -> str:
    status, payload = request_json(
        base_url,
        "POST",
        "/api/workflow-runs",
        {
            "workflow_definition_id": definition_id,
            "input": {
                "_meta": {"reply": {"mode": "webhook", "callback_url": callback_url, "correlation_id": correlation_id}}
            },
        },
    )
    assert status == 202, payload
    return payload["run"]["id"]


def request_json(base_url: str, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base_url + path, data=body, method=method, headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def wait_for_audit(runtime: AtlasRuntime, action: str, resource_id: str, timeout: float = 2) -> dict:
    """update_delivery() and audit() are separate, sequential writes (no shared transaction), so
    a delivery's new status can be observable a beat before its audit entry commits. Poll
    instead of asserting on a single snapshot."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for entry in runtime.db.list_audit(limit=1000):
            if entry["action"] == action and entry["resource_id"] == resource_id:
                return entry
        time.sleep(0.02)
    raise AssertionError(f"no {action} audit entry for {resource_id}")


def wait_for_run(runtime: AtlasRuntime, run_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        run = runtime.db.get_workflow_run(run_id)
        if run and run["state"] in {"succeeded", "failed", "cancelled"}:
            assert run["state"] == "succeeded", run
            return run
        time.sleep(0.02)
    raise AssertionError(f"workflow run {run_id} did not finish: {runtime.db.get_workflow_run(run_id)}")


def wait_for_delivery(runtime: AtlasRuntime, run_id: str, status: str, timeout: float = 5) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        deliveries = runtime.db.list_deliveries(limit=10, run_id=run_id)
        if deliveries:
            last = deliveries[0]
            if last["status"] == status:
                return last
        time.sleep(0.02)
    raise AssertionError(f"delivery for run {run_id} did not reach {status}: {last}")


if __name__ == "__main__":
    main()
