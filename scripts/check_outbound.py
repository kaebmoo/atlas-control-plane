"""OB-1 hermetic check (docs/plans/input-adapter-return-path-plan.md).

Verifies the signed outbound delivery return path: a run completing with `_meta.reply.webhook`
delivers a signed POST to the callback (correct run_id/state/correlation_id/artifacts); a
non-allowlisted/private callback is `blocked` and never sent; a receiver that keeps failing is
retried up to `ATLAS_OUTBOUND_MAX_ATTEMPTS` then dead-lettered as `failed` WITHOUT touching the
run's own outcome, and the same `delivery_id` is reused across every attempt (dedupable);
`POST /api/deliveries/{id}/retry` re-attempts a `failed` delivery within the bound; a missing
`ATLAS_SECRET_KEY` refuses to send unsigned and records why.
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

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length)
        MockReceiverHandler.received.append(
            {"path": self.path, "signature": self.headers.get("X-Atlas-Signature"), "raw": raw, "body": json.loads(raw or b"{}")}
        )
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

            # GET /api/deliveries lists everything created above, filterable by run_id/status.
            # (run_a also carries the synthetic `blocked_seed` delivery from scenario B.)
            status, listed = request_json(base_url, "GET", f"/api/deliveries?run_id={run_a}")
            assert status == 200 and {d["id"] for d in listed["deliveries"]} == {delivery_a["id"], blocked_seed["id"]}, listed
            status, failed_listed = request_json(base_url, "GET", "/api/deliveries?status=blocked")
            assert status == 200 and {d["id"] for d in failed_listed["deliveries"]} >= {blocked_seed["id"], delivery_d["id"]}
        finally:
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
