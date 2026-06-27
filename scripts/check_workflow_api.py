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
            check_milestone_7(runtime, base_url, workflow_id)
            check_milestone_8(base_url)

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
            check_milestones_3_and_4(base_url, workflow_id)
            check_milestone_5(base_url)
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


def check_milestones_3_and_4(base_url: str, workflow_id: str) -> None:
    source = request(
        base_url,
        "POST",
        "/api/workflows",
        {
            "name": "Event source",
            "graph": {"start": "source", "nodes": [{"id": "source", "type": "worker", "prompt": "source"}], "edges": []},
        },
    )["workflow"]

    webhook = request(
        base_url,
        "POST",
        "/api/workflow-triggers",
        {"workflow_definition_id": workflow_id, "name": "Webhook", "type": "webhook"},
    )["trigger"]
    fired = request(
        base_url,
        "POST",
        f"/api/workflow-triggers/{webhook['id']}/fire",
        {"payload": {"topic": "webhook"}, "dedupe_key": "webhook-once"},
    )
    assert fired["run"] and wait_for_api_run(base_url, fired["run"]["id"], "failed")["state"] == "failed"
    duplicate = request(
        base_url,
        "POST",
        f"/api/workflow-triggers/{webhook['id']}/fire",
        {"payload": {"topic": "webhook"}, "dedupe_key": "webhook-once"},
    )
    assert duplicate["event"]["state"] == "ignored"
    listed = next(trigger for trigger in request(base_url, "GET", "/api/workflow-triggers")["triggers"] if trigger["id"] == webhook["id"])
    assert listed["last_event_state"] == "ignored"
    assert "duplicate dedupe_key" in listed["last_event_error"]

    completion = request(
        base_url,
        "POST",
        "/api/workflow-triggers",
        {
            "workflow_definition_id": workflow_id,
            "name": "After source",
            "type": "workflow_run_completed",
            "config": {"source_workflow_definition_id": source["id"], "state": "failed"},
        },
    )["trigger"]
    source_run = request(base_url, "POST", "/api/workflow-runs", {"workflow_definition_id": source["id"]})["run"]
    wait_for_api_run(base_url, source_run["id"], "failed")
    completion_event = wait_for_trigger_event(base_url, completion["id"], "started")
    assert completion_event["run_id"] != source_run["id"]
    assert completion_event["payload"]["run_id"] == source_run["id"]
    assert completion_event["payload"]["event_type"] == "workflow_run_completed"
    internal_fire = request_error(base_url, "POST", f"/api/workflow-triggers/{completion['id']}/fire", {})
    assert "fired by Atlas events" in internal_fire["error"]

    artifact_trigger = request(
        base_url,
        "POST",
        "/api/workflow-triggers",
        {
            "workflow_definition_id": workflow_id,
            "name": "Invoice artifact",
            "type": "artifact_created",
            "config": {"source_workflow_definition_id": source["id"], "key": "invoice"},
        },
    )["trigger"]
    created = request(
        base_url,
        "POST",
        "/api/artifacts",
        {
            "run_id": source_run["id"],
            "key": "invoice",
            "kind": "json",
            "content": {"total": 3},
            "metadata": {"source": "manual"},
        },
    )["artifact"]
    assert created["content"] == {"total": 3}
    assert created["metadata"] == {"source": "manual"}
    fetched = request(base_url, "GET", f"/api/artifacts/{created['id']}")["artifact"]
    assert fetched["content"] == {"total": 3}
    artifacts = request(base_url, "GET", f"/api/workflow-runs/{source_run['id']}/artifacts")["artifacts"]
    assert artifacts[0]["content"] == {"total": 3}
    artifact_event = wait_for_trigger_event(base_url, artifact_trigger["id"], "started")
    assert artifact_event["payload"]["artifact_id"] == created["id"]
    assert artifact_event["payload"]["key"] == "invoice"
    bad_kind = request_error(
        base_url,
        "POST",
        "/api/artifacts",
        {"run_id": source_run["id"], "key": "bad", "kind": "binary", "content": "x"},
    )
    assert "unsupported artifact kind" in bad_kind["error"]
    bad_json = request_error(
        base_url,
        "POST",
        "/api/artifacts",
        {"run_id": source_run["id"], "key": "bad-json", "kind": "json", "content": "{"},
    )
    assert "valid JSON" in bad_json["error"]

    worker = request(
        base_url,
        "POST",
        "/api/workers",
        {"name": "Offline event worker", "base_url": "http://127.0.0.1:1"},
    )["worker"]
    worker_trigger = request(
        base_url,
        "POST",
        "/api/workflow-triggers",
        {
            "workflow_definition_id": workflow_id,
            "name": "Worker offline",
            "type": "worker_status_changed",
            "config": {"worker_id": worker["id"], "status": "offline"},
        },
    )["trigger"]
    assert request(base_url, "POST", f"/api/workers/{worker['id']}/poll")["worker"]["status"] == "offline"
    wait_for_trigger_event(base_url, worker_trigger["id"], "started")
    started_count = sum(event["state"] == "started" for event in request(base_url, "GET", f"/api/workflow-triggers/{worker_trigger['id']}/events")["events"])
    request(base_url, "POST", f"/api/workers/{worker['id']}/poll")
    assert sum(event["state"] == "started" for event in request(base_url, "GET", f"/api/workflow-triggers/{worker_trigger['id']}/events")["events"]) == started_count == 1


def check_milestone_5(base_url: str) -> None:
    workflow = request(
        base_url,
        "POST",
        "/api/workflows",
        {
            "name": "Human gate API",
            "graph": {
                "start": "gate",
                "nodes": [{"id": "gate", "type": "human_gate", "label": "Approve API run"}],
                "edges": [],
            },
        },
    )["workflow"]

    run = request(base_url, "POST", "/api/workflow-runs", {"workflow_definition_id": workflow["id"]})["run"]
    run = wait_for_api_run(base_url, run["id"], "waiting_for_human")
    pending = request(base_url, "GET", f"/api/approvals?state=pending&run_id={run['id']}")["approvals"]
    assert len(pending) == 1 and pending[0]["node_key"] == "gate"
    detail = request(base_url, "GET", f"/api/workflow-runs/{run['id']}")
    assert detail["nodes"][0]["state"] == "waiting_for_human"
    assert detail["nodes"][0]["job_id"] is None
    assert detail["approvals"][0]["id"] == pending[0]["id"]

    approved = request(base_url, "POST", f"/api/approvals/{pending[0]['id']}/approve")
    assert approved["approval"]["state"] == "approved"
    assert wait_for_api_run(base_url, run["id"], "succeeded")["state"] == "succeeded"
    duplicate = request_error(base_url, "POST", f"/api/approvals/{pending[0]['id']}/approve")
    assert "already approved" in duplicate["error"]

    rejected_run = request(base_url, "POST", "/api/workflow-runs", {"workflow_definition_id": workflow["id"]})["run"]
    wait_for_api_run(base_url, rejected_run["id"], "waiting_for_human")
    rejected_approval = request(base_url, "GET", f"/api/approvals?state=pending&run_id={rejected_run['id']}")["approvals"][0]
    rejected = request(base_url, "POST", f"/api/approvals/{rejected_approval['id']}/reject")
    assert rejected["approval"]["state"] == "rejected"
    assert rejected["run"]["state"] == "failed"
    duplicate = request_error(base_url, "POST", f"/api/approvals/{rejected_approval['id']}/reject")
    assert "already rejected" in duplicate["error"]
    event_types = {event["event_type"] for event in request(base_url, "GET", f"/api/workflow-runs/{rejected_run['id']}/events")["events"]}
    assert {"approval_created", "approval_rejected", "node_failed", "run_finished"} <= event_types


def check_milestone_7(runtime: AtlasRuntime, base_url: str, workflow_id: str) -> None:
    builder = runtime.db.upsert_worker(
        {"name": "Workflow Builder", "base_url": "http://127.0.0.1:2", "role": "workflow_builder"}
    )
    original_submit = runtime.jobs.submit
    response = {"text": ""}
    prompts: list[str] = []

    def submit(payload: dict) -> dict:
        prompts.append(payload["prompt"])
        job = runtime.db.create_job(
            {"worker_id": builder["id"], "prompt": payload["prompt"], "state": "running"}
        )
        runtime.db.append_job_text(job["id"], response["text"])
        runtime.db.update_job(job["id"], state="succeeded", finished_at=now_iso())
        return runtime.db.get_job(job["id"]) or job

    runtime.jobs.submit = submit
    valid_draft = {
        "name": "Builder draft",
        "description": "validated",
        "graph": {
            "start": "gate",
            "nodes": [
                {"id": "gate", "type": "human_gate", "label": "Approve"},
                {"id": "join", "type": "join", "mode": "all"},
            ],
            "edges": [{"from": "gate", "to": "join", "condition": {"type": "always"}}],
        },
        "policy": {"max_jobs": 2},
        "triggers": [{"name": "Every 15", "type": "schedule", "config": {"interval_minutes": 15}}],
        "explanation": "A bounded draft.",
        "warnings": [],
    }
    try:
        response["text"] = json.dumps(valid_draft)
        draft = request(
            base_url,
            "POST",
            "/api/workflows/draft",
            {"plain_language_prompt": "gate then join every 15 minutes"},
        )["draft"]
        assert draft["triggers"][0]["config"]["interval_minutes"] == 15
        assert all(value in prompts[-1] for value in ["human_gate", "manager", "join", "artifact_kinds", "policy_defaults"])

        invalid_schedule = dict(valid_draft, triggers=[{"type": "schedule", "config": {"interval_minutes": 0}}])
        response["text"] = json.dumps(invalid_schedule)
        assert "interval_minutes must be positive" in request_error(
            base_url, "POST", "/api/workflows/draft", {"plain_language_prompt": "bad schedule"}
        )["error"]

        outside_dsl = dict(
            valid_draft,
            graph={"start": "x", "nodes": [{"id": "x", "type": "tool"}], "edges": []},
            triggers=[],
        )
        response["text"] = json.dumps(outside_dsl)
        assert "unsupported type" in request_error(
            base_url, "POST", "/api/workflows/draft", {"plain_language_prompt": "outside DSL"}
        )["error"]

        response["text"] = json.dumps({"explanation": "Builder explanation."})
        assert request(base_url, "POST", f"/api/workflows/{workflow_id}/explain")["explanation"] == "Builder explanation."

        response["text"] = "not JSON"
        bad_repair = request_error(
            base_url,
            "POST",
            f"/api/workflows/{workflow_id}/repair",
            {"graph": {"start": "x", "nodes": [{"id": "x", "type": "tool"}], "edges": []}},
        )
        assert "must be one JSON object" in bad_repair["error"]

        response["text"] = json.dumps(
            {"triggers": [{"name": "Morning", "type": "schedule", "config": {"daily_time": "09:30"}}]}
        )
        suggestions = request(
            base_url,
            "POST",
            f"/api/workflows/{workflow_id}/suggest-triggers",
            {"plain_language_prompt": "run each morning"},
        )["triggers"]
        assert suggestions == [{"name": "Morning", "type": "schedule", "config": {"daily_time": "09:30"}}]
    finally:
        runtime.jobs.submit = original_submit


def check_milestone_8(base_url: str) -> None:
    for index, role in enumerate(["reporter", "fact_checker", "editor", "anchor", "researcher", "writer", "reviewer", "coder", "tester", "manager"], 10):
        request(
            base_url,
            "POST",
            "/api/workers",
            {"name": role, "base_url": f"http://127.0.0.1:{index}", "role": role},
        )

    templates = request(base_url, "GET", "/api/workflow-templates")["templates"]
    assert [template["name"] for template in templates] == [
        "News Desk",
        "Researcher -> Writer -> Reviewer",
        "Coder -> Tester -> Reviewer",
        "Manager-directed loop with max 3 iterations",
    ]
    manager = templates[-1]
    assert manager["policy"]["max_iterations"] == 3
    assert all(
        edge["condition"]["type"] == "manager_selected"
        for edge in manager["graph"]["edges"]
        if edge["from"] == "manager"
    )
    for template in templates:
        created = request(
            base_url,
            "POST",
            "/api/workflows",
            {key: template[key] for key in ["name", "description", "graph", "policy"]},
        )["workflow"]
        assert created["graph"] == template["graph"]
        assert created["policy"] == template["policy"]

    html = request_text(base_url, "/")
    javascript = request_text(base_url, "/static/app.js")
    assert 'id="workflowTemplateSelect"' in html
    assert "template.graph" in javascript and "template.policy" in javascript


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


def request_text(base_url: str, path: str) -> str:
    with urllib.request.urlopen(base_url + path, timeout=5) as response:
        return response.read().decode("utf-8")


def wait_for_api_run(base_url: str, run_id: str, state: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        run = request(base_url, "GET", f"/api/workflow-runs/{run_id}")["run"]
        if run["state"] == state:
            return run
        time.sleep(0.02)
    raise AssertionError(f"workflow run {run_id} did not reach {state}")


def wait_for_trigger_event(base_url: str, trigger_id: str, state: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        events = request(base_url, "GET", f"/api/workflow-triggers/{trigger_id}/events")["events"]
        event = next((item for item in events if item["state"] == state), None)
        if event:
            return event
        time.sleep(0.02)
    raise AssertionError(f"workflow trigger {trigger_id} did not record {state}")


if __name__ == "__main__":
    main()
