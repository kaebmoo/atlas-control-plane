from __future__ import annotations

import json
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.db import Database
from atlas.jobs import JobManager

# Flipped by the health check to simulate a healthy vs reachable-but-unhealthy worker.
WORKER_OK = {"value": True}


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


def main() -> None:
    with TemporaryDirectory() as tmp:
        check_submit_routing_failure_no_orphan(Database(Path(tmp) / "orphan.sqlite"))
        check_poll_worker_health(Database(Path(tmp) / "health.sqlite"))
    print("jobs check ok")


if __name__ == "__main__":
    main()
