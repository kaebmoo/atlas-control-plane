from __future__ import annotations

import http.client
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
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
                upload_dir=Path(tmp) / "uploads",
                max_upload_bytes=32,
                outbound_allowlist=("127.0.0.1",),
            )
        )
        server = AtlasHttpServer(("127.0.0.1", 0), runtime)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            invalid = request_error(base_url, "POST", "/api/workflows", {"status": "active", "name": "bad", "graph": {"nodes": []}})
            assert "non-empty list" in invalid["error"]
            bad_worker = request_error(
                base_url,
                "POST",
                "/api/workflows",
                {
                    "status": "active",
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
                    "status": "active",
                    "name": "bad policy",
                    "graph": {"start": "only", "nodes": [{"id": "only", "type": "worker"}], "edges": []},
                    "policy": {"max_jobs": 1000},
                },
            )
            assert "policy max_jobs" in bad_policy["error"]
            # name is OPTIONAL on create (OpenAPI WorkflowCreateInput requires only graph):
            # a name-less create must succeed with the documented "Untitled workflow" default,
            # not be rejected — that is the additive contract.
            unnamed = request(
                base_url,
                "POST",
                "/api/workflows",
                {"graph": {"start": "only", "nodes": [{"id": "only", "type": "worker"}], "edges": []}, "policy": {"max_jobs": 1}},
            )["workflow"]
            assert unnamed["name"] == "Untitled workflow", unnamed
            request(base_url, "DELETE", f"/api/workflows/{unnamed['id']}")  # keep the list clean for ordering-sensitive checks below

            # Legacy collect_files on a non-job node remains save-compatible, but the API must
            # make its no-op runtime meaning visible. (Mutations, both proven red: drop the
            # `warnings` key from the save/validate responses -> KeyError; invert
            # workflow_graph_warnings' node-type predicate -> the list comes back empty.)
            legacy_graph = {"start": "gate", "nodes": [{"id": "gate", "type": "human_gate", "collect_files": ["out.md"]}], "edges": []}
            legacy = request(base_url, "POST", "/api/workflows", {"status": "active", "name": "Legacy collection", "graph": legacy_graph})
            assert len(legacy["warnings"]) == 1 and "gate" in legacy["warnings"][0] and "ignored" in legacy["warnings"][0], legacy
            assert request(base_url, "POST", f"/api/workflows/{legacy['workflow']['id']}/validate")["warnings"] == legacy["warnings"]
            updated_legacy = request(base_url, "PUT", f"/api/workflows/{legacy['workflow']['id']}", {"description": "still compatible"})
            assert updated_legacy["warnings"] == legacy["warnings"], updated_legacy
            # Reads carry the same report, so a client that never saves the definition (the ops
            # console lists them and shows the banner on a run) still sees it. Inside each
            # workflow object — a list response cannot carry a per-item sibling key.
            listed = next(
                row for row in request(base_url, "GET", "/api/workflows")["workflows"]
                if row["id"] == legacy["workflow"]["id"]
            )
            assert listed["warnings"] == legacy["warnings"], listed["warnings"]
            fetched = request(base_url, "GET", f"/api/workflows/{legacy['workflow']['id']}")["workflow"]
            assert fetched["warnings"] == legacy["warnings"], fetched["warnings"]
            # A clean definition reports an empty list, never a missing key.
            clean = request(
                base_url, "POST", "/api/workflows",
                {"status": "active", "name": "Clean graph", "graph": {"start": "only", "nodes": [{"id": "only", "type": "worker"}], "edges": []}},
            )["workflow"]
            assert request(base_url, "GET", f"/api/workflows/{clean['id']}")["workflow"]["warnings"] == []
            request(base_url, "DELETE", f"/api/workflows/{clean['id']}")
            request(base_url, "DELETE", f"/api/workflows/{legacy['workflow']['id']}")

            workflow = request(
                base_url,
                "POST",
                "/api/workflows",
                {
                    "status": "active",
                    "name": "API smoke",
                    "graph": {
                        "start": "only",
                        "nodes": [{"id": "only", "type": "worker", "prompt": "Topic: {input.topic}", "outputs": ["notes"]}],
                        "edges": [],
                    },
                    "policy": {"max_jobs": 1},
                    "default_reply": {"mode": "webhook", "callback_url": "https://127.0.0.1/reply/default"},
                },
            )["workflow"]
            workflow_id = workflow["id"]
            assert workflow["default_reply"] == {"mode": "webhook", "callback_url": "https://127.0.0.1/reply/default"}
            assert request(base_url, "GET", "/api/workflows")["workflows"][0]["id"] == workflow_id
            assert request(base_url, "GET", f"/api/workflows/{workflow_id}")["workflow"]["default_reply"] == workflow["default_reply"]
            assert request(base_url, "POST", f"/api/workflows/{workflow_id}/validate")["ok"]

            rejected_default_reply = request_error(
                base_url,
                "POST",
                "/api/workflows",
                {
                    "status": "active",
                    "name": "blocked reply",
                    "graph": {"start": "only", "nodes": [{"id": "only", "type": "worker"}], "edges": []},
                    "default_reply": {"mode": "webhook", "callback_url": "https://10.1.2.3/reply"},
                },
            )
            assert "default_reply.callback_url is not deliverable" in rejected_default_reply["error"], rejected_default_reply

            updated = request(base_url, "PUT", f"/api/workflows/{workflow_id}", {"description": "updated"})["workflow"]
            assert updated["description"] == "updated"
            updated = request(base_url, "PUT", f"/api/workflows/{workflow_id}", {"default_reply": {"mode": "none"}})["workflow"]
            assert updated["default_reply"] == {"mode": "none"}
            assert request(base_url, "GET", f"/api/workflows/{workflow_id}")["workflow"]["default_reply"] == {"mode": "none"}
            rejected_default_reply_update = request_error(
                base_url,
                "PUT",
                f"/api/workflows/{workflow_id}",
                {"default_reply": {"mode": "webhook", "callback_url": "https://10.1.2.3/reply"}},
            )
            assert "default_reply.callback_url is not deliverable" in rejected_default_reply_update["error"], rejected_default_reply_update
            # PUT must reject a non-integer version (would later break pack export) and a
            # status carrying unsafe characters; a valid integer version is accepted.
            bad_version = request_error(base_url, "PUT", f"/api/workflows/{workflow_id}", {"version": "1.0.0"})
            assert "integer" in bad_version["error"], bad_version
            bad_status = request_error(base_url, "PUT", f"/api/workflows/{workflow_id}", {"status": 'x" onmouseover="y'})
            assert "status" in bad_status["error"], bad_status
            assert request(base_url, "PUT", f"/api/workflows/{workflow_id}", {"version": 3})["workflow"]["version"] == 3
            conditional = request(
                base_url, "PUT", f"/api/workflows/{workflow_id}", {"name": "conditional", "expected_version": 3}
            )["workflow"]
            assert conditional["version"] == 4 and conditional["name"] == "conditional"
            conflict = request_error(
                base_url,
                "PUT",
                f"/api/workflows/{workflow_id}",
                {"name": "stale", "expected_version": 3},
                status=409,
            )
            assert "version conflict" in conflict["error"]
            assert "starts at only" in request(base_url, "POST", f"/api/workflows/{workflow_id}/explain")["explanation"]
            assert request(base_url, "POST", f"/api/workflows/{workflow_id}/repair")["draft"]["explanation"] == "Workflow already validates."

            bad_input = request_error(base_url, "POST", "/api/workflow-runs", {"workflow_definition_id": workflow_id, "input": [1, 2]})
            assert "input must be an object" in bad_input["error"], bad_input

            inherited = request(base_url, "POST", "/api/workflow-runs", {"workflow_definition_id": workflow_id, "input": {"topic": "inherited"}})["run"]
            assert inherited["input"]["_meta"]["reply"] == {"mode": "none"}, inherited
            updated = request(base_url, "PUT", f"/api/workflows/{workflow_id}", {"default_reply": {"mode": "webhook", "callback_url": "https://127.0.0.1/reply/default"}})["workflow"]
            assert updated["default_reply"]["mode"] == "webhook"
            overridden = request(
                base_url,
                "POST",
                "/api/workflow-runs",
                {"workflow_definition_id": workflow_id, "input": {"_meta": {"reply": {"mode": "none"}}}},
            )["run"]
            assert overridden["input"]["_meta"]["reply"] == {"mode": "none"}, overridden
            request(base_url, "PUT", f"/api/workflows/{workflow_id}", {"default_reply": {"mode": "none"}})

            # A stored default that a later allowlist change made undeliverable (simulated by
            # writing it at the db layer, which does not re-validate) must reject ONLY a run
            # that would inherit it — a run supplying its own reply still starts.
            runtime.db.update_workflow_definition(workflow_id, {"default_reply": {"mode": "webhook", "callback_url": "https://10.9.9.9/stale"}})
            stale_default = request_error(base_url, "POST", "/api/workflow-runs", {"workflow_definition_id": workflow_id, "input": {"topic": "x"}})
            assert "default_reply.callback_url is not deliverable" in stale_default["error"], stale_default
            override_ok = request(
                base_url,
                "POST",
                "/api/workflow-runs",
                {"workflow_definition_id": workflow_id, "input": {"_meta": {"reply": {"mode": "none"}}}},
            )["run"]
            assert override_ok["input"]["_meta"]["reply"] == {"mode": "none"}, override_ok
            request(base_url, "PUT", f"/api/workflows/{workflow_id}", {"default_reply": {"mode": "none"}})

            # PUT policy:null must not 500 (regression: NULL write into a NOT NULL column);
            # it clears the policy, which reads back as null and is treated as {} downstream.
            cleared = request(base_url, "PUT", f"/api/workflows/{workflow_id}", {"policy": None})["workflow"]
            assert cleared["policy"] is None, cleared
            request(base_url, "PUT", f"/api/workflows/{workflow_id}", {"policy": {"max_jobs": 1}})

            run = request(base_url, "POST", "/api/workflow-runs", {"workflow_definition_id": workflow_id, "input": {"topic": "x"}})["run"]
            run = wait_for_api_run(base_url, run["id"], "failed")
            detail = request(base_url, "GET", f"/api/workflow-runs/{run['id']}")
            assert detail["run"]["id"] == run["id"]
            assert detail["nodes"][0]["state"] == "failed"
            assert request(base_url, "GET", f"/api/workflow-runs/{run['id']}/artifacts")["artifacts"] == []
            run_events = request(base_url, "GET", f"/api/workflow-runs/{run['id']}/events")["events"]
            assert [event["seq"] for event in run_events] == list(range(1, len(run_events) + 1))
            assert {event["event_type"] for event in run_events} >= {"created", "node_started", "node_failed", "run_finished"}
            first_page = request(base_url, "GET", f"/api/workflow-runs/{run['id']}/events?limit=1")
            assert first_page["after"] == 0 and first_page["has_more"] is True
            assert first_page["next_after"] == first_page["events"][0]["seq"]
            second_page = request(
                base_url, "GET", f"/api/workflow-runs/{run['id']}/events?limit=1&after={first_page['next_after']}"
            )
            assert second_page["events"][0]["seq"] > first_page["next_after"]
            assert "non-negative" in request_error(base_url, "GET", f"/api/workflow-runs/{run['id']}/events?after=-1")["error"]

            check_run_hold(base_url, workflow_id)
            check_upload_rejected_after_run_finished(runtime, base_url, workflow_id)

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
            check_milestones_9_and_10(base_url)
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
            assert fired["run"]["input"]["_meta"]["reply"] == {"mode": "none"}, fired
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
            # An absurd interval overflows timedelta — it must be a clean 400, not an HTTP 500.
            assert "interval_minutes is too large" in request_error(
                base_url, "POST", "/api/workflow-triggers",
                {"workflow_definition_id": workflow_id, "name": "Huge", "type": "schedule", "config": {"interval_minutes": 1e15}},
            )["error"]
            # A sub-second interval rounds to next_fire_at == now (never advances) — reject as 400.
            assert "at least 1 second" in request_error(
                base_url, "POST", "/api/workflow-triggers",
                {"workflow_definition_id": workflow_id, "name": "TooSmall", "type": "schedule", "config": {"interval_minutes": 0.001}},
            )["error"]
            # NaN (JSON admits it) slips a plain <= 0 check — must be a clean domain 400, not a leak.
            assert "must be positive" in request_error(
                base_url, "POST", "/api/workflow-triggers",
                {"workflow_definition_id": workflow_id, "name": "NaN", "type": "schedule", "config": {"interval_minutes": float("nan")}},
            )["error"]
            # The 1-second boundary (1/60 min) is the smallest interval that advances — accepted.
            boundary_trigger = request(
                base_url, "POST", "/api/workflow-triggers",
                {"workflow_definition_id": workflow_id, "name": "OneSecond", "type": "schedule", "config": {"interval_minutes": 1 / 60}},
            )["trigger"]
            assert boundary_trigger["next_fire_at"]
            disabled = request(base_url, "PUT", f"/api/workflow-triggers/{schedule['id']}", {"enabled": False})["trigger"]
            assert not disabled["enabled"]
            assert schedule["id"] not in {item["id"] for item in runtime.db.list_workflow_triggers(enabled=True)}
            # A disabled trigger fired via the direct API path must be ignored, not started.
            fired_disabled = request(
                base_url,
                "POST",
                f"/api/workflow-triggers/{schedule['id']}/fire",
                {"payload": {"topic": "nope"}},
            )
            assert fired_disabled["event"]["state"] == "ignored", fired_disabled["event"]
            assert fired_disabled["run"] is None, fired_disabled
            check_milestones_3_and_4(base_url, workflow_id)
            check_milestone_5(base_url)
            check_milestone_14(runtime, base_url)
            check_milestones_13_and_15(runtime, base_url)
            assert request(base_url, "DELETE", f"/api/workflow-triggers/{schedule['id']}")["deleted"]
            assert request(base_url, "DELETE", f"/api/workflow-triggers/{trigger['id']}")["deleted"]
            assert request(base_url, "DELETE", f"/api/workflows/{workflow_id}")["deleted"]

            bad = request_error(base_url, "POST", f"/api/workflows/{workflow_id}/validate", status=404)
            assert bad["error"] == "not found"
        finally:
            runtime.close()  # stop the reaper daemon before the tempdir exits
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    print("workflow api check ok")


def check_run_hold(base_url: str, workflow_id: str) -> None:
    """`POST /api/workflow-runs {"hold": true}` creates the run born-paused with NO execution,
    so input files can be attached race-free before an explicit resume starts it.
    (Mutations: ignore `hold` in start_workflow → the run executes immediately → the
    paused/no-nodes asserts go red; skip the isinstance guard → `"hold": "yes"` stops 400ing.)"""
    held = request(base_url, "POST", "/api/workflow-runs", {"workflow_definition_id": workflow_id, "hold": True})["run"]
    assert held["state"] == "paused", held["state"]
    # A runner wrongly spawned would flip the state and write runtime nodes almost
    # immediately; the sleep gives that mutation time to go red, not the feature time to work.
    time.sleep(0.3)
    detail = request(base_url, "GET", f"/api/workflow-runs/{held['id']}")
    assert detail["run"]["state"] == "paused", detail["run"]["state"]
    assert detail["nodes"] == [], f"a held run must not dispatch nodes: {detail['nodes']}"
    events = request(base_url, "GET", f"/api/workflow-runs/{held['id']}/events")["events"]
    assert any(event["event_type"] == "run_created_held" for event in events), [e["event_type"] for e in events]
    # files attach while held (the whole point of the hold window). 8 bytes stays under the
    # harness's max_upload_bytes=32.
    uploaded = request_binary(
        base_url,
        "POST",
        f"/api/workflow-runs/{held['id']}/files?key=upload_brief",
        body=b"brief!!\n",
        headers={"X-Filename": "brief.txt", "Content-Type": "text/plain", "Content-Length": "8"},
    )
    assert uploaded["json"]["artifact"]["kind"] == "file_ref", uploaded["json"]
    # a plain ASCII X-Filename passes through the RFC 3986 decode unchanged...
    assert uploaded["json"]["artifact"]["metadata"]["filename"] == "brief.txt", uploaded["json"]
    # ...and a percent-encoded non-ASCII one (browser fetch cannot send Thai bytes in a header,
    # so clients MUST encode) is stored as the real decoded name. (Mutation: drop the unquote()
    # in _upload_workflow_file → the stored name stays percent-encoded → red.)
    thai_name = "แผนวิสาหกิจ 70-74.pdf"
    uploaded_thai = request_binary(
        base_url,
        "POST",
        f"/api/workflow-runs/{held['id']}/files?key=upload_plan_pdf",
        body=b"pdf-like",
        headers={
            "X-Filename": urllib.parse.quote(thai_name),
            "Content-Type": "application/pdf",
            "Content-Length": "8",
        },
    )
    assert uploaded_thai["json"]["artifact"]["metadata"]["filename"] == thai_name, uploaded_thai["json"]
    # resume starts execution: the run leaves paused and reaches this graph's usual terminal
    # state (failed — the harness has no live worker), proving the engine actually ran it.
    resumed = request(base_url, "POST", f"/api/workflow-runs/{held['id']}/resume", {})["run"]
    assert resumed["state"] != "paused", resumed["state"]
    final = wait_for_api_run(base_url, held["id"], "failed")
    assert final["state"] == "failed"
    # a non-boolean hold is rejected up front.
    bad = request_error(base_url, "POST", "/api/workflow-runs", {"workflow_definition_id": workflow_id, "hold": "yes"})
    assert "hold" in bad["error"], bad


def check_upload_rejected_after_run_finished(runtime: AtlasRuntime, base_url: str, workflow_id: str) -> None:
    """A file attached to a finished run can never be pushed to a worker or read by a node, so
    it is a stranded artifact that reads to the user like a successful attachment: reject it with
    409 instead of 201. Runs that have NOT finished keep accepting uploads.
    (Mutation: drop the terminal-state guard in _upload_workflow_file -> the upload 201s -> red.)"""
    run = request(base_url, "POST", "/api/workflow-runs", {"workflow_definition_id": workflow_id})["run"]
    finished = wait_for_api_run(base_url, run["id"], "failed")  # no live worker in this harness
    assert finished["state"] == "failed", finished
    rejected = request_binary(
        base_url,
        "POST",
        f"/api/workflow-runs/{run['id']}/files?key=too_late",
        body=b"late",
        headers={"Content-Type": "application/octet-stream", "X-Filename": "late.bin", "Content-Length": "4"},
        expect_error=True,
    )
    assert rejected["status"] == 409, rejected["status"]
    assert "already finished" in rejected["json"]["error"] and "failed" in rejected["json"]["error"], rejected["json"]
    assert not request(base_url, "GET", f"/api/artifacts?run_id={run['id']}&kind=file_ref")["artifacts"], (
        "the rejected upload must leave no artifact behind"
    )
    # A cancelled run is terminal too.
    cancelled = request(base_url, "POST", "/api/workflow-runs", {"workflow_definition_id": workflow_id})["run"]
    request(base_url, "POST", f"/api/workflow-runs/{cancelled['id']}/cancel")
    rejected_cancelled = request_binary(
        base_url,
        "POST",
        f"/api/workflow-runs/{cancelled['id']}/files?key=too_late",
        body=b"late",
        headers={"Content-Type": "application/octet-stream", "X-Filename": "late.bin", "Content-Length": "4"},
        expect_error=True,
    )
    assert rejected_cancelled["status"] == 409, rejected_cancelled["status"]

    # TOCTOU: the pre-read state check is stale by the time the body finishes streaming. Send the
    # headers, cancel the run while the body is still open, then finish the body — the insert must
    # re-check and reject. (Mutation: swap create_artifact_for_live_run back to create_artifact ->
    # the upload 201s and strands a file_ref on a cancelled run -> red.)
    gate_graph = {"start": "hold", "nodes": [{"id": "hold", "type": "human_gate", "label": "Hold"}], "edges": []}
    gated_workflow = request(base_url, "POST", "/api/workflows", {"status": "active", "name": "Upload TOCTOU", "graph": gate_graph})["workflow"]
    live = request(base_url, "POST", "/api/workflow-runs", {"workflow_definition_id": gated_workflow["id"]})["run"]
    wait_for_api_run(base_url, live["id"], "waiting_for_human")
    parsed = urllib.parse.urlparse(base_url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    connection.putrequest("POST", f"/api/workflow-runs/{live['id']}/files?key=racing")
    connection.putheader("Content-Length", "8")
    connection.putheader("Content-Type", "application/octet-stream")
    connection.putheader("X-Filename", "racing.bin")
    connection.endheaders()
    connection.send(b"half")
    request(base_url, "POST", f"/api/workflow-runs/{live['id']}/cancel")
    connection.send(b"rest")
    response = connection.getresponse()
    raced = json.loads(response.read())
    connection.close()
    assert response.status == 409, f"expected HTTP 409 after the run was cancelled mid-upload, got {response.status}"
    assert "already finished" in raced["error"], raced
    assert not request(base_url, "GET", f"/api/artifacts?run_id={live['id']}&kind=file_ref")["artifacts"], (
        "a file_ref must not land on a run cancelled mid-upload"
    )
    assert not any(path.name.endswith(".tmp") for path in runtime.upload_dir.iterdir()), "staged upload left behind"


def check_milestones_3_and_4(base_url: str, workflow_id: str) -> None:
    source = request(
        base_url,
        "POST",
        "/api/workflows",
        {
            "status": "active",
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
    # Global listing (GET /api/artifacts): windowed newest-first + a truthful total. Assertions
    # stay per-response (membership, filter correctness, window size) — never "is the newest"
    # — because triggered runs may be creating artifacts concurrently with these reads.
    listing = request(base_url, "GET", "/api/artifacts")
    assert listing["total"] >= 1 and listing["limit"] == 100
    assert created["id"] in {artifact["id"] for artifact in listing["artifacts"]}
    filtered = request(base_url, "GET", f"/api/artifacts?run_id={source_run['id']}&kind=json&key=invoice")
    assert created["id"] in {artifact["id"] for artifact in filtered["artifacts"]}
    assert all(
        artifact["run_id"] == source_run["id"] and artifact["kind"] == "json" and artifact["key"] == "invoice"
        for artifact in filtered["artifacts"]
    )
    assert filtered["total"] == len(filtered["artifacts"])  # filters narrow below the window
    windowed = request(base_url, "GET", "/api/artifacts?limit=1")
    assert len(windowed["artifacts"]) == 1 and windowed["limit"] == 1 and windowed["total"] >= 1
    bad_list_kind = request_error(base_url, "GET", "/api/artifacts?kind=binary")
    assert "unsupported artifact kind" in bad_list_kind["error"]
    # Backward-compatible default and the selector contract are table-driven so every
    # supported spelling is exercised against a row carrying a unique payload marker.
    marker = "SECRET-LISTING-MARKER-9f1c"
    marked = request(
        base_url,
        "POST",
        "/api/artifacts",
        {
            "run_id": source_run["id"],
            "key": "listing-marker",
            "kind": "text",
            "content": marker,
            "metadata": {"content": "metadata-key-is-not-the-payload"},
        },
    )["artifact"]
    selector_cases = [
        ("default", "", True),
        ("include_content=true", "include_content=true", True),
        ("include_content=false", "include_content=false", False),
        ("view=full", "view=full", True),
        ("view=metadata", "view=metadata", False),
        ("matching full selectors", "include_content=true&view=full", True),
        ("matching metadata selectors", "include_content=false&view=metadata", False),
    ]
    for label, selectors, has_content in selector_cases:
        suffix = f"&{selectors}" if selectors else ""
        listing = request(base_url, "GET", f"/api/artifacts?run_id={source_run['id']}{suffix}")
        row = next((artifact for artifact in listing["artifacts"] if artifact["id"] == marked["id"]), None)
        assert row is not None, f"{label} selector omitted the marked row"
        if has_content:
            assert row["content"] == marker, label
        else:
            assert "content" not in row, label

    for label, selectors in [
        ("metadata view with include_content=true", "include_content=true&view=metadata"),
        ("full view with include_content=false", "include_content=false&view=full"),
    ]:
        conflict = request_error(base_url, "GET", f"/api/artifacts?{selectors}")
        assert "conflicts" in conflict["error"], label

    for label, selectors in [
        ("invalid include_content", "include_content=maybe"),
        ("invalid view", "view=compact"),
    ]:
        invalid_selector = request_error(base_url, "GET", f"/api/artifacts?{selectors}")
        assert "must be" in invalid_selector["error"], label

    raw_listing = request_binary(base_url, "GET", f"/api/artifacts?run_id={source_run['id']}&include_content=false")
    assert marked["id"] in {artifact["id"] for artifact in raw_listing["json"]["artifacts"]}
    assert marker.encode() not in raw_listing["body"], "metadata listing must not serialize artifact content"
    assert all("content" not in artifact for artifact in raw_listing["json"]["artifacts"])
    assert request(base_url, "GET", f"/api/artifacts/{marked['id']}")["artifact"]["content"] == marker
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
            "status": "active",
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

    choice_workflow = request(
        base_url,
        "POST",
        "/api/workflows",
        {
            "status": "active",
            "name": "Choice gate",
            "graph": {
                "start": "gate",
                "nodes": [
                    {"id": "gate", "type": "human_gate", "choices": [{"id": "finish", "label": "Finish"}]},
                    {"id": "done", "type": "join", "mode": "all"},
                ],
                "edges": [{"from": "gate", "to": "done", "condition": {"type": "human_selected", "choice": "finish"}}],
            },
        },
    )["workflow"]
    choice_run = request(base_url, "POST", "/api/workflow-runs", {"workflow_definition_id": choice_workflow["id"]})["run"]
    choice_approval = wait_for_pending_approval(base_url, choice_run["id"])
    assert choice_approval["choices"] == [{"id": "finish", "label": "Finish"}]
    chosen = request(base_url, "POST", f"/api/approvals/{choice_approval['id']}/choose", {"choice": "finish"})
    assert chosen["approval"]["selected_choice"] == "finish"
    assert wait_for_api_run(base_url, choice_run["id"], "succeeded")["state"] == "succeeded"
    duplicate_choice = request_error(base_url, "POST", f"/api/approvals/{choice_approval['id']}/choose", {"choice": "finish"})
    assert "already chosen" in duplicate_choice["error"]


def check_milestones_9_and_10(base_url: str) -> None:
    reporter = request(
        base_url,
        "POST",
        "/api/workers",
        {"name": "Suggestion Reporter", "base_url": "http://127.0.0.1:40", "tags": ["reporter"]},
    )["worker"]
    graph = {
        "start": "report",
        "nodes": [
            {"id": "report", "type": "worker", "role": "reporter", "prompt": "report"},
            {"id": "missing", "type": "worker", "role": "not_configured", "prompt": "missing"},
        ],
        "edges": [{"from": "report", "to": "missing"}],
    }
    suggestions = request(
        base_url,
        "POST",
        "/api/workflows/suggest-workers",
        {"graph": graph, "policy": {"allowed_worker_ids": [reporter["id"]]}},
    )["suggestions"]
    assert suggestions[0]["state"] == "matched" and suggestions[0]["worker_id"] == reporter["id"]
    assert suggestions[1]["state"] == "unavailable" and "No configured worker" in suggestions[1]["reason"]

    # The embedded UI is a minimal ops console; workflow editing/AI-assist surfaces live in the
    # external frontend, so only the security-sensitive UI markers are asserted here.
    javascript = request_text(base_url, "/static/app.js")
    # Security regression guards: status is whitelist-sanitized before it reaches a class
    # attribute (stored-XSS), and artifact downloads go through an authenticated fetch
    # rather than a token-less <a href> (401 under auth).
    assert "replace(/[^A-Za-z0-9-]/g" in javascript, "statusClass must whitelist-sanitize the status token"
    assert "downloadArtifact" in javascript and 'href="/api/artifacts/' not in javascript, "artifact download must use authenticated fetch"
    # Job stream must use an authenticated fetch (Bearer header), never a token in the URL, and
    # must surface a disconnect when the body EOFs without the server's close event.
    assert "token=${encodeURIComponent" not in javascript, "job stream must not put the API token in the URL"
    # Lock the EOF-without-close detection specifically (the bare error string also lives in the
    # catch path, so assert the sawClose guard that distinguishes a clean close from a drop).
    assert "sawClose" in javascript and "if (!sawClose)" in javascript, "job stream must detect EOF without a close event"


def check_milestone_7(runtime: AtlasRuntime, base_url: str, workflow_id: str) -> None:
    builder = runtime.db.upsert_worker(
        {"name": "Workflow Builder", "base_url": "http://127.0.0.1:2", "role": "workflow_builder"}
    )
    original_submit = runtime.jobs.submit
    response = {"text": ""}
    # A queue for multi-call scenarios (draft self-repair retry): each submit pops the next
    # reply; when empty it falls back to response["text"] so single-reply cases stay unchanged.
    response_queue: list[str] = []
    fail_job = {"flag": False}
    prompts: list[str] = []

    def submit(payload: dict, *, on_created=None) -> dict:
        prompts.append(payload["prompt"])
        job = runtime.db.create_job(
            {"worker_id": builder["id"], "prompt": payload["prompt"], "state": "running"}
        )
        if fail_job["flag"]:
            runtime.db.update_job(job["id"], state="failed", error="builder worker exploded", finished_at=now_iso())
            return runtime.db.get_job(job["id"]) or job
        runtime.db.append_job_text(job["id"], response_queue.pop(0) if response_queue else response["text"])
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
        # The context must spell out nested field contracts, not just field names: a real
        # builder model guessed human_gate choices as plain strings ("choice requires id")
        # because nothing in the context showed the object shape or per-condition fields.
        assert all(
            fragment in prompts[-1]
            for fragment in (
                "choices_item",
                "declared choice ids",
                "must equal the edge's to node",
                "required positive integer",
            )
        ), "builder context must include the nested choices/condition contracts"

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

        # Draft validation must be schema-equivalent: falsy/wrong-typed fields that `or {}`/`or []`
        # would otherwise coerce must be rejected, and warning items must be strings.
        for bad, fragment in (
            (dict(valid_draft, policy=[]), "policy must be an object"),
            (dict(valid_draft, triggers=None), "triggers must be a list"),
            (dict(valid_draft, warnings=[1]), "warnings must be a list of strings"),
        ):
            response["text"] = json.dumps(bad)
            assert fragment in request_error(
                base_url, "POST", "/api/workflows/draft", {"plain_language_prompt": "bad draft"}
            )["error"], fragment

        # Bounded self-repair: a first draft with the classic wrong choices shape (plain strings)
        # is fed back to the builder exactly once; the corrected second reply comes out with a
        # retry note appended to warnings.
        gate_draft_bad = json.loads(json.dumps(valid_draft))
        gate_draft_bad["graph"]["nodes"][0] = {"id": "gate", "type": "human_gate", "label": "Approve", "choices": ["approve", "revise"]}
        gate_draft_good = json.loads(json.dumps(valid_draft))
        gate_draft_good["graph"]["nodes"][0] = {
            "id": "gate",
            "type": "human_gate",
            "label": "Approve",
            "choices": [{"id": "approve", "label": "Approve"}, {"id": "revise", "label": "Revise"}],
        }
        gate_draft_good["graph"]["edges"] = [
            {"from": "gate", "to": "join", "condition": {"type": "human_selected", "choice": "approve"}}
        ]
        calls_before = len(prompts)
        response_queue[:] = [json.dumps(gate_draft_bad), json.dumps(gate_draft_good)]
        repaired = request(
            base_url, "POST", "/api/workflows/draft", {"plain_language_prompt": "gate with approve/revise choices"}
        )["draft"]
        assert len(prompts) == calls_before + 2, "self-repair must spend exactly one extra builder call"
        assert "choice requires id" in prompts[-1], "retry prompt must carry the deterministic validation error"
        assert "Previous draft JSON" in prompts[-1], "retry prompt must carry the rejected draft"
        assert repaired["graph"]["nodes"][0]["choices"][0] == {"id": "approve", "label": "Approve"}
        assert any("self-repair" in warning for warning in repaired["warnings"]), "retry must be surfaced in warnings"

        # A reply that is not one JSON object is the same retry-worthy class as a validation
        # failure — one bounded retry, then success.
        calls_before = len(prompts)
        response_queue[:] = ["```json\n{\"name\": \"fenced\"}\n```", json.dumps(valid_draft)]
        retried = request(
            base_url, "POST", "/api/workflows/draft", {"plain_language_prompt": "fenced reply"}
        )["draft"]
        assert len(prompts) == calls_before + 2
        assert any("self-repair" in warning for warning in retried["warnings"])

        # Persistently invalid output still fails closed after exactly two calls, and the error
        # is the deterministic validation message, not retry mechanics.
        calls_before = len(prompts)
        response_queue[:] = [json.dumps(gate_draft_bad), json.dumps(gate_draft_bad)]
        persistent = request_error(
            base_url, "POST", "/api/workflows/draft", {"plain_language_prompt": "still bad"}
        )
        assert "choice requires id" in persistent["error"]
        assert len(prompts) == calls_before + 2, "retry is bounded to exactly one extra call"

        # Infrastructure failures (the builder job itself fails) must NOT spend a second model
        # call — a retry cannot fix an offline worker or a dead key.
        calls_before = len(prompts)
        fail_job["flag"] = True
        infra = request_error(base_url, "POST", "/api/workflows/draft", {"plain_language_prompt": "job dies"})
        fail_job["flag"] = False
        assert "workflow_builder job failed" in infra["error"]
        assert len(prompts) == calls_before + 1, "a failed builder job must not trigger a retry"
        assert not response_queue, "all queued replies must have been consumed"

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

        unresolved_graph = {
            "start": "report",
            "nodes": [{"id": "report", "type": "worker", "role": "reporter", "prompt": "report"}],
            "edges": [],
        }
        response["text"] = json.dumps(
            {"suggestions": [{"node_id": "report", "role": "reporter", "worker_id": "wrk_invented", "reason": "x", "state": "matched"}]}
        )
        assert "invented worker_id" in request_error(
            base_url, "POST", "/api/workflows/suggest-workers", {"graph": unresolved_graph}
        )["error"]

        response["text"] = json.dumps(
            {"suggestions": [{"node_id": "report", "role": "reporter", "worker_id": builder["id"], "reason": "x", "state": "fallback"}]}
        )
        allowed = next(worker for worker in runtime.db.list_workers() if "reporter" in (worker.get("tags") or []))
        assert "policy-forbidden worker_id" in request_error(
            base_url,
            "POST",
            "/api/workflows/suggest-workers",
            {"graph": unresolved_graph, "policy": {"allowed_worker_ids": [allowed["id"]]}},
        )["error"]
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


def check_milestone_14(runtime: AtlasRuntime, base_url: str) -> None:
    control_graph = {"start": "done", "nodes": [{"id": "done", "type": "join", "mode": "all"}], "edges": []}
    # The upload host run parks at a human gate: uploads are only accepted while a run is still
    # live (a finished run 409s — check_upload_rejected_after_run_finished), and this check is
    # about the upload/trigger/purge behavior, not about the run's state.
    gate_graph = {"start": "hold", "nodes": [{"id": "hold", "type": "human_gate", "label": "Hold for uploads"}], "edges": []}
    source = request(base_url, "POST", "/api/workflows", {"status": "active", "name": "Upload source", "graph": gate_graph})["workflow"]
    target = request(base_url, "POST", "/api/workflows", {"status": "active", "name": "Upload target", "graph": control_graph})["workflow"]
    run = request(base_url, "POST", "/api/workflow-runs", {"workflow_definition_id": source["id"]})["run"]
    wait_for_api_run(base_url, run["id"], "waiting_for_human")
    trigger = request(
        base_url,
        "POST",
        "/api/workflow-triggers",
        {
            "workflow_definition_id": target["id"],
            "name": "Uploaded",
            "type": "artifact_created",
            "config": {"source_workflow_definition_id": source["id"], "key": "attachment", "kind": "file_ref"},
        },
    )["trigger"]

    content = b"\x00atlas-file\xff"
    uploaded = request_binary(
        base_url,
        "POST",
        f"/api/workflow-runs/{run['id']}/files?key=attachment",
        content,
        {"Content-Type": "application/octet-stream", "X-Filename": "../../evidence.bin"},
    )["json"]["artifact"]
    assert uploaded["kind"] == "file_ref" and uploaded["metadata"]["filename"] == "evidence.bin"
    assert uploaded["metadata"]["size"] == len(content) and len(uploaded["metadata"]["sha256"]) == 64
    downloaded = request_binary(base_url, "GET", f"/api/artifacts/{uploaded['id']}/content")
    assert downloaded["body"] == content and "evidence.bin" in downloaded["headers"]["Content-Disposition"]
    # A file_ref's content column holds the opaque stored-file id; the metadata-only global
    # listing opt-in must not leak it either (rows carry no content property at all).
    file_rows = request(base_url, "GET", f"/api/artifacts?run_id={run['id']}&kind=file_ref&include_content=false")[
        "artifacts"
    ]
    assert uploaded["id"] in {row["id"] for row in file_rows}
    assert all("content" not in row for row in file_rows), "file_ref listing must not expose the stored-file id"
    wait_for_trigger_event(base_url, trigger["id"], "started")
    events = request(base_url, "GET", f"/api/workflow-triggers/{trigger['id']}/events")["events"]
    assert sum(event["state"] == "received" for event in events) == 1

    oversized = request_binary(
        base_url,
        "POST",
        f"/api/workflow-runs/{run['id']}/files?key=large",
        b"x" * 33,
        {"Content-Type": "application/octet-stream", "X-Filename": "large.bin"},
        expect_error=True,
    )
    assert "exceeds maximum" in oversized["json"]["error"]
    manual = request(
        base_url, "POST", "/api/artifacts", {"run_id": run["id"], "key": "text", "kind": "text", "content": "not a file"}
    )["artifact"]
    assert "not a file_ref" in request_error(base_url, "GET", f"/api/artifacts/{manual['id']}/content")["error"]

    parsed = urllib.parse.urlparse(base_url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
    connection.putrequest("POST", f"/api/workflow-runs/{run['id']}/files?key=incomplete")
    connection.putheader("Content-Length", "10")
    connection.putheader("Content-Type", "application/octet-stream")
    connection.endheaders()
    connection.send(b"short")
    connection.sock.shutdown(1)
    response = connection.getresponse()
    incomplete = json.loads(response.read())
    connection.close()
    assert "incomplete" in incomplete["error"]
    assert not any(path.name.endswith(".tmp") for path in runtime.upload_dir.iterdir())


def check_milestones_13_and_15(runtime: AtlasRuntime, base_url: str) -> None:
    invalid_quorum = request_error(
        base_url,
        "POST",
        "/api/workflows",
        {
            "status": "active",
            "name": "Invalid quorum",
            "graph": {
                "start": "join",
                "nodes": [{"id": "join", "type": "join", "mode": "quorum", "quorum": 1}],
                "edges": [],
            },
        },
    )
    assert "quorum exceeds distinct incoming" in invalid_quorum["error"]

    worker = runtime.db.list_workers()[0]
    definition = runtime.db.create_workflow_definition(
        {
            "status": "active",
            "name": "API recovery",
            "graph": {
                "start": "work",
                "nodes": [{"id": "work", "type": "worker", "worker_id": worker["id"], "prompt": "recover"}],
                "edges": [],
            },
        }
    )

    def interrupted_run() -> dict:
        run = runtime.db.create_workflow_run(
            {
                "workflow_definition_id": definition["id"],
                "state": "running",
                "current_nodes": ["work"],
                "counters": {"jobs_started": 0, "budget_units_spent": 0, "node_counts": {}, "completed_nodes": []},
                "started_at": now_iso(),
            }
        )
        job = runtime.db.create_job({"worker_id": worker["id"], "prompt": "old", "state": "running"})
        runtime.db.create_workflow_node({"run_id": run["id"], "node_key": "work", "state": "running", "job_id": job["id"], "attempt": 1})
        return run

    run = interrupted_run()
    runtime.workflows.reconcile_runs()
    detail = request(base_url, "GET", f"/api/workflow-runs/{run['id']}")
    assert detail["run"]["state"] == "recovery_required"
    assert detail["run"]["counters"]["recovery"]["interrupted"][0]["job_id"]
    assert "retry_interrupted authorization" in request_error(
        base_url, "POST", f"/api/workflow-runs/{run['id']}/resume", {}
    )["error"]

    original_submit = runtime.jobs.submit

    def submit(payload: dict, *, on_created=None) -> dict:
        job = runtime.db.create_job({"worker_id": worker["id"], "prompt": payload["prompt"], "state": "running"})
        runtime.db.append_job_text(job["id"], "recovered")
        runtime.db.update_job(job["id"], state="succeeded", finished_at=now_iso())
        return runtime.db.get_job(job["id"]) or job

    runtime.jobs.submit = submit
    try:
        request(base_url, "POST", f"/api/workflow-runs/{run['id']}/resume", {"retry_interrupted": True})
        assert wait_for_api_run(base_url, run["id"], "succeeded")["state"] == "succeeded"
    finally:
        runtime.jobs.submit = original_submit

    cancelled = interrupted_run()
    runtime.workflows.reconcile_runs()
    assert request(base_url, "POST", f"/api/workflow-runs/{cancelled['id']}/cancel")["run"]["state"] == "cancelled"
    html = request_text(base_url, "/")
    javascript = request_text(base_url, "/static/app.js")
    assert 'id="retryInterruptedRunBtn"' in html and "retry_interrupted: true" in javascript


def request(base_url: str, method: str, path: str, payload: dict | None = None) -> dict:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base_url + path, data=body, method=method, headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def request_binary(
    base_url: str,
    method: str,
    path: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    expect_error: bool = False,
) -> dict:
    req = urllib.request.Request(base_url + path, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            raw = response.read()
            content_type = response.headers.get("Content-Type", "")
            return {
                "status": response.status,
                "body": raw,
                "json": json.loads(raw) if raw and "json" in content_type else {},
                "headers": dict(response.headers),
            }
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        if not expect_error:
            raise
        # `status` matters as much as the message: a 500 whose str() happens to match would
        # otherwise satisfy a message-only assertion (same rationale as request_error).
        return {"status": exc.code, "body": raw, "json": json.loads(raw), "headers": dict(exc.headers)}


def request_error(base_url: str, method: str, path: str, payload: dict | None = None, status: int = 400) -> dict:
    """Expect the request to fail with exactly `status` (default 400) and return the error body.
    Asserting the code matters: without it a 500 carrying the same message (e.g. an unhandled
    exception whose str() matches the expected ValueError text) would satisfy a message-only
    assertion and hide a server bug."""
    try:
        request(base_url, method, path, payload)
    except urllib.error.HTTPError as exc:
        assert exc.code == status, f"{method} {path}: expected HTTP {status}, got {exc.code}"
        return json.loads(exc.read().decode("utf-8"))
    raise AssertionError(f"{method} {path}: expected HTTP {status}, request succeeded")


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


def wait_for_pending_approval(base_url: str, run_id: str) -> dict:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        approvals = request(base_url, "GET", f"/api/approvals?state=pending&run_id={run_id}")["approvals"]
        if approvals:
            return approvals[0]
        time.sleep(0.01)
    raise AssertionError(f"workflow run {run_id} did not create an approval")


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
