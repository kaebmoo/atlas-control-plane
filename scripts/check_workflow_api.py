from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.app import AtlasHttpServer, AtlasRuntime
from atlas.config import Config
from atlas.db import now_iso


def main() -> None:
    with TemporaryDirectory() as tmp:
        runtime = AtlasRuntime(
            Config(
                host="127.0.0.1",
                port=0,
                db_path=Path(tmp) / "atlas.sqlite",
                api_token=None,
                request_timeout_seconds=1,
                enable_loopback_without_token=True,
            )
        )
        server = AtlasHttpServer(("127.0.0.1", 0), runtime)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            invalid = request_error(base_url, "POST", "/api/workflows", {"name": "bad", "graph": {"nodes": []}})
            assert "non-empty list" in invalid["error"]
            bad_worker = request_error(
                base_url,
                "POST",
                "/api/workflows",
                {
                    "name": "bad worker",
                    "graph": {"start": "only", "nodes": [{"id": "only", "type": "worker", "worker_id": "wrk_missing"}], "edges": []},
                },
            )
            assert "unknown worker_id" in bad_worker["error"]
            bad_policy = request_error(
                base_url,
                "POST",
                "/api/workflows",
                {
                    "name": "bad policy",
                    "graph": {"start": "only", "nodes": [{"id": "only", "type": "worker"}], "edges": []},
                    "policy": {"max_jobs": 1000},
                },
            )
            assert "policy max_jobs" in bad_policy["error"]

            workflow = request(
                base_url,
                "POST",
                "/api/workflows",
                {
                    "name": "API smoke",
                    "graph": {
                        "start": "only",
                        "nodes": [{"id": "only", "type": "worker", "prompt": "Topic: {input.topic}", "outputs": ["notes"]}],
                        "edges": [],
                    },
                    "policy": {"max_jobs": 1},
                },
            )["workflow"]
            workflow_id = workflow["id"]
            assert request(base_url, "GET", "/api/workflows")["workflows"][0]["id"] == workflow_id
            assert request(base_url, "POST", f"/api/workflows/{workflow_id}/validate")["ok"]

            updated = request(base_url, "PUT", f"/api/workflows/{workflow_id}", {"description": "updated"})["workflow"]
            assert updated["description"] == "updated"
            assert "starts at only" in request(base_url, "POST", f"/api/workflows/{workflow_id}/explain")["explanation"]
            assert request(base_url, "POST", f"/api/workflows/{workflow_id}/repair")["draft"]["explanation"] == "Workflow already validates."

            run = request(base_url, "POST", "/api/workflow-runs", {"workflow_definition_id": workflow_id, "input": {"topic": "x"}})["run"]
            run = wait_for_api_run(base_url, run["id"], "failed")
            detail = request(base_url, "GET", f"/api/workflow-runs/{run['id']}")
            assert detail["run"]["id"] == run["id"]
            assert detail["nodes"][0]["state"] == "failed"
            assert request(base_url, "GET", f"/api/workflow-runs/{run['id']}/artifacts")["artifacts"] == []
            run_events = request(base_url, "GET", f"/api/workflow-runs/{run['id']}/events")["events"]
            assert [event["seq"] for event in run_events] == list(range(1, len(run_events) + 1))
            assert {event["event_type"] for event in run_events} >= {"created", "node_started", "node_failed", "run_finished"}

            paused = runtime.db.create_workflow_run(
                {
                    "workflow_definition_id": workflow_id,
                    "name": "Paused API run",
                    "state": "running",
                    "current_nodes": ["only"],
                    "started_at": now_iso(),
                }
            )
            assert request(base_url, "POST", f"/api/workflow-runs/{paused['id']}/pause")["run"]["state"] == "paused"
            request(base_url, "POST", f"/api/workflow-runs/{paused['id']}/resume")
            assert wait_for_api_run(base_url, paused["id"], "failed")["state"] == "failed"

            cancelled = runtime.db.create_workflow_run(
                {"workflow_definition_id": workflow_id, "name": "Cancelled API run", "state": "running"}
            )
            assert request(base_url, "POST", f"/api/workflow-runs/{cancelled['id']}/cancel")["run"]["state"] == "cancelled"

            draft_error = request_error(base_url, "POST", "/api/workflows/draft", {"plain_language_prompt": "make a news workflow"})
            assert "workflow_builder" in draft_error["error"]

            trigger = request(
                base_url,
                "POST",
                "/api/workflow-triggers",
                {"workflow_definition_id": workflow_id, "name": "Manual", "type": "manual"},
            )["trigger"]
            assert request(base_url, "GET", "/api/workflow-triggers")["triggers"][0]["id"] == trigger["id"]
            fired = request(
                base_url,
                "POST",
                f"/api/workflow-triggers/{trigger['id']}/fire",
                {"payload": {"topic": "manual"}, "dedupe_key": "once"},
            )
            assert wait_for_api_run(base_url, fired["run"]["id"], "failed")["state"] == "failed"
            ignored = request(
                base_url,
                "POST",
                f"/api/workflow-triggers/{trigger['id']}/fire",
                {"payload": {"topic": "manual"}, "dedupe_key": "once"},
            )
            assert ignored["event"]["state"] == "ignored"
            events = request(base_url, "GET", f"/api/workflow-triggers/{trigger['id']}/events")["events"]
            assert {event["state"] for event in events} >= {"received", "started", "ignored"}

            schedule = request(
                base_url,
                "POST",
                "/api/workflow-triggers",
                {"workflow_definition_id": workflow_id, "name": "Interval", "type": "schedule", "config": {"interval_minutes": 5}},
            )["trigger"]
            assert schedule["next_fire_at"]
            disabled = request(base_url, "PUT", f"/api/workflow-triggers/{schedule['id']}", {"enabled": False})["trigger"]
            assert not disabled["enabled"]
            assert request(base_url, "DELETE", f"/api/workflow-triggers/{schedule['id']}")["deleted"]
            assert request(base_url, "DELETE", f"/api/workflow-triggers/{trigger['id']}")["deleted"]
            assert request(base_url, "DELETE", f"/api/workflows/{workflow_id}")["deleted"]

            bad = request_error(base_url, "POST", f"/api/workflows/{workflow_id}/validate")
            assert bad["error"] == "not found"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    print("workflow api check ok")


def request(base_url: str, method: str, path: str, payload: dict | None = None) -> dict:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base_url + path, data=body, method=method, headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def request_error(base_url: str, method: str, path: str, payload: dict | None = None) -> dict:
    try:
        return request(base_url, method, path, payload)
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read().decode("utf-8"))
    raise AssertionError("expected HTTPError")


def wait_for_api_run(base_url: str, run_id: str, state: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        run = request(base_url, "GET", f"/api/workflow-runs/{run_id}")["run"]
        if run["state"] == state:
            return run
        time.sleep(0.02)
    raise AssertionError(f"workflow run {run_id} did not reach {state}")


if __name__ == "__main__":
    main()
