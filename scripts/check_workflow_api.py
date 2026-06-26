from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.app import AtlasHttpServer, AtlasRuntime
from atlas.config import Config


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
            assert run["state"] == "failed"
            detail = request(base_url, "GET", f"/api/workflow-runs/{run['id']}")
            assert detail["run"]["id"] == run["id"]
            assert detail["nodes"][0]["state"] == "failed"
            assert request(base_url, "GET", f"/api/workflow-runs/{run['id']}/artifacts")["artifacts"] == []

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
            assert fired["run"]["state"] == "failed"
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


if __name__ == "__main__":
    main()
