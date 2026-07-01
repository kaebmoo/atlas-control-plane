from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.db import Database, now_iso
from atlas.router import Router
from atlas.workflows import MAX_TRIGGER_CHAIN_DEPTH, WorkflowRunner, _trigger_chain_blocks, render_prompt, validate_workflow_graph


def main() -> None:
    graph = {
        "start": "reporter",
        "nodes": [
            {"id": "reporter", "type": "worker", "prompt": "Topic: {input.topic}", "outputs": ["notes"]},
            {"id": "anchor", "type": "worker", "prompt": "Read: {artifact.notes}", "outputs": ["script"]},
        ],
        "edges": [{"from": "reporter", "to": "anchor", "condition": {"type": "always"}}],
    }
    assert validate_workflow_graph(graph, {}) is graph

    duplicate = dict(graph, nodes=graph["nodes"] + [{"id": "reporter", "type": "worker"}])
    assert_raises("duplicate node id: reporter", validate_workflow_graph, duplicate, {})

    missing_edge_target = dict(graph, edges=[{"from": "reporter", "to": "missing", "condition": {"type": "always"}}])
    assert_raises("missing to node: missing", validate_workflow_graph, missing_edge_target, {})

    bad_condition = dict(graph, edges=[{"from": "reporter", "to": "anchor", "condition": {"type": "unsupported"}}])
    assert_raises("unsupported condition", validate_workflow_graph, bad_condition, {})

    bad_join = dict(graph, nodes=graph["nodes"] + [{"id": "join", "type": "join", "mode": "quorum"}])
    assert_raises("quorum must be a positive integer", validate_workflow_graph, bad_join, {})

    human_gate = {"start": "gate", "nodes": [{"id": "gate", "type": "human_gate", "label": "Approve"}], "edges": []}
    assert validate_workflow_graph(human_gate, {}) is human_gate
    choice_gate = {
        "start": "gate",
        "nodes": [
            {"id": "gate", "type": "human_gate", "choices": [{"id": "left", "label": "Left"}]},
            {"id": "left", "type": "worker"},
        ],
        "edges": [{"from": "gate", "to": "left", "condition": {"type": "human_selected", "choice": "left"}}],
    }
    assert validate_workflow_graph(choice_gate, {}) is choice_gate
    assert_raises(
        "choice is not declared",
        validate_workflow_graph,
        dict(choice_gate, edges=[{"from": "gate", "to": "left", "condition": {"type": "human_selected", "choice": "missing"}}]),
        {},
    )

    bad_artifact_condition = dict(graph, edges=[{"from": "reporter", "to": "anchor", "condition": {"type": "artifact_equals"}}])
    assert_raises("artifact_equals requires artifact", validate_workflow_graph, bad_artifact_condition, {})

    cycle = dict(graph, edges=graph["edges"] + [{"from": "anchor", "to": "reporter", "condition": {"type": "always"}}])
    assert_raises("policy.max_iterations or max_iterations_below", validate_workflow_graph, cycle, {})
    assert validate_workflow_graph(cycle, {"max_iterations": 2}) is cycle
    guarded_cycle = dict(
        graph,
        edges=graph["edges"] + [{"from": "anchor", "to": "reporter", "condition": {"type": "max_iterations_below", "node": "reporter", "max": 2}}],
    )
    assert validate_workflow_graph(guarded_cycle, {}) is guarded_cycle

    prompt = render_prompt(
        'Return JSON: {"verdict":"ok"}\nTopic: {input.topic}\nNotes: {artifact.notes}\nRun: {run.id}\nPrev: {job.previous.assistant_text}',
        input={"topic": "weather"},
        artifacts=[{"key": "notes", "content": "cloudy"}],
        run={"id": "run_1"},
        node={"id": "reporter"},
        job={"previous": {"assistant_text": "old"}},
    )
    assert '{"verdict":"ok"}' in prompt
    assert "Topic: weather" in prompt
    assert "Notes: cloudy" in prompt
    assert "Run: run_1" in prompt
    assert "Prev: old" in prompt

    assert_raises("missing prompt variable: {input.missing}", render_prompt, "{input.missing}", input={})
    check_role_routing()
    check_runner()
    check_joins_and_fan_out()
    check_condition_runner()
    check_managers()
    check_budget_and_failure_policy()
    check_human_gates()
    check_hardening()
    check_recovery()
    check_trigger_chain_guard()
    print("workflow validation/render check ok")


def check_trigger_chain_guard() -> None:
    # Event-driven trigger cycles must be rejected: direct self-trigger, longer cycles
    # (A->B->A where the target already appears in the chain), and runaway depth.
    assert _trigger_chain_blocks("A", "A", [])                 # direct self-trigger
    assert _trigger_chain_blocks("A", "B", ["A", "B"])         # cycle back to A
    assert not _trigger_chain_blocks("B", "A", ["A"])          # legitimate A -> B hop
    assert not _trigger_chain_blocks("C", "B", ["A", "B"])     # fresh target is allowed
    assert _trigger_chain_blocks("Z", "Y", [str(i) for i in range(MAX_TRIGGER_CHAIN_DEPTH)])  # depth cap


def check_runner() -> None:
    with TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "atlas.sqlite")
        worker = db.upsert_worker({"name": "Fake", "base_url": "http://127.0.0.1:1"})
        graph = {
            "start": "reporter",
            "nodes": [
                {"id": "reporter", "type": "worker", "worker_id": worker["id"], "prompt": "Topic: {input.topic}", "outputs": ["notes"]},
                {"id": "anchor", "type": "worker", "worker_id": worker["id"], "role": "anchor", "prompt": "Read: {artifact.notes}", "outputs": ["script"]},
            ],
            "edges": [{"from": "reporter", "to": "anchor", "condition": {"type": "always"}}],
        }
        definition = db.create_workflow_definition({"name": "Linear", "graph": graph, "policy": {"max_jobs": 5}})
        fake_jobs = FakeJobService(db, worker["id"])
        run = WorkflowRunner(db, fake_jobs, poll_interval_seconds=0).run_workflow(definition["id"], {"topic": "weather"})

        assert run["state"] == "succeeded"
        assert run["current_nodes"] == []
        assert fake_jobs.prompts == ["Topic: weather", "Read: result: Topic: weather"]
        assert fake_jobs.payloads[1]["role"] == "anchor"
        assert [node["state"] for node in db.list_workflow_nodes(run["id"])] == ["succeeded", "succeeded"]
        assert [edge["to_node"] for edge in db.list_workflow_edges(run["id"])] == ["anchor"]
        assert {artifact["key"]: artifact["content"] for artifact in db.list_artifacts(run_id=run["id"])} == {
            "notes": "result: Topic: weather",
            "script": "result: Read: result: Topic: weather",
        }
        events = db.list_workflow_events(run["id"])
        assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
        assert {event["event_type"] for event in events} >= {
            "created",
            "node_started",
            "node_succeeded",
            "edge_taken",
            "run_finished",
        }


def check_joins_and_fan_out() -> None:
    with TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "atlas.sqlite")
        worker = db.upsert_worker({"name": "Fake", "base_url": "http://127.0.0.1:1"})
        nodes = [
            {"id": "root", "type": "worker", "prompt": "root"},
            {"id": "left", "type": "worker", "prompt": "left"},
            {"id": "right", "type": "worker", "prompt": "right"},
            {"id": "join", "type": "join", "mode": "all"},
            {"id": "done", "type": "worker", "prompt": "done"},
        ]
        edges = [
            {"from": "root", "to": "left"},
            {"from": "root", "to": "right"},
            {"from": "left", "to": "join"},
            {"from": "right", "to": "join"},
            {"from": "join", "to": "done"},
        ]

        all_jobs = FakeJobService(db, worker["id"])
        all_run = WorkflowRunner(db, all_jobs, poll_interval_seconds=0).run_graph(
            {"start": "root", "nodes": nodes, "edges": edges}
        )
        assert all_jobs.prompts == ["root", "left", "right", "done"]
        assert all_run["counters"]["completed_nodes"] == ["root", "left", "right", "join", "done"]
        assert all_run["counters"]["join_states"]["join"] == {
            "mode": "all",
            "state": "succeeded",
            "upstream_nodes": ["left", "right"],
            "completed_upstreams": ["left", "right"],
        }
        join_nodes = [node for node in db.list_workflow_nodes(all_run["id"]) if node["node_key"] == "join"]
        assert len(join_nodes) == 1 and join_nodes[0]["state"] == "succeeded" and join_nodes[0]["job_id"] is None
        all_events = [(event["event_type"], event["node_key"]) for event in db.list_workflow_events(all_run["id"])]
        assert all_events.index(("node_succeeded", "right")) < all_events.index(("node_started", "join"))

        any_jobs = FakeJobService(db, worker["id"])
        any_run = WorkflowRunner(db, any_jobs, poll_interval_seconds=0).run_graph(
            {"start": "root", "nodes": [dict(node, mode="any") if node["id"] == "join" else node for node in nodes], "edges": edges}
        )
        assert any_jobs.prompts == ["root", "left", "done", "right"]
        any_events = [(event["event_type"], event["node_key"]) for event in db.list_workflow_events(any_run["id"])]
        assert any_events.index(("node_started", "done")) < any_events.index(("node_started", "right"))
        assert any_run["counters"]["join_states"]["join"]["state"] == "succeeded"

        duplicate_jobs = FakeJobService(db, worker["id"])
        duplicate_run = WorkflowRunner(db, duplicate_jobs, poll_interval_seconds=0).run_graph(
            {
                "start": "root",
                "nodes": [node for node in nodes if node["id"] != "join"],
                "edges": [edges[0], edges[1], {"from": "left", "to": "done"}, {"from": "right", "to": "done"}, {"from": "right", "to": "done"}],
            }
        )
        assert duplicate_jobs.prompts == ["root", "left", "right", "done"]
        assert duplicate_run["counters"]["node_counts"]["done"] == 1

        resume_graph = {
            "start": "first",
            "nodes": [{"id": "first", "type": "worker", "prompt": "first"}, {"id": "second", "type": "worker", "prompt": "second"}],
            "edges": [{"from": "first", "to": "second"}],
        }
        definition = db.create_workflow_definition({"name": "Resume completed", "graph": resume_graph})
        paused = db.create_workflow_run(
            {
                "workflow_definition_id": definition["id"],
                "state": "paused",
                "current_nodes": ["first", "second"],
                "counters": {"jobs_started": 1, "node_counts": {"first": 1}, "completed_nodes": ["first"]},
                "started_at": now_iso(),
            }
        )
        resume_jobs = FakeJobService(db, worker["id"])
        resume_runner = WorkflowRunner(db, resume_jobs, poll_interval_seconds=0)
        resume_runner.resume_run(paused["id"])
        resumed = wait_for_run(db, paused["id"], "succeeded")
        wait_for_runner_stopped(resume_runner, paused["id"])
        assert resume_jobs.prompts == ["second"]
        assert resumed["counters"]["completed_nodes"] == ["first", "second"]

        quorum_nodes = [
            {"id": "root", "type": "worker", "worker_id": worker["id"], "prompt": "root"},
            {"id": "left", "type": "worker", "worker_id": worker["id"], "prompt": "left"},
            {"id": "right", "type": "worker", "worker_id": worker["id"], "prompt": "right"},
            {"id": "third", "type": "worker", "worker_id": worker["id"], "prompt": "third"},
            {"id": "quorum", "type": "join", "mode": "quorum", "quorum": 2},
            {"id": "done", "type": "worker", "worker_id": worker["id"], "prompt": "done"},
        ]
        quorum_edges = [
            {"from": "root", "to": "left"}, {"from": "root", "to": "right"}, {"from": "root", "to": "third"},
            {"from": "left", "to": "quorum"}, {"from": "left", "to": "quorum"},
            {"from": "right", "to": "quorum"}, {"from": "third", "to": "quorum"},
            {"from": "quorum", "to": "done"},
        ]
        quorum_jobs = FakeJobService(db, worker["id"])
        quorum_run = WorkflowRunner(db, quorum_jobs, poll_interval_seconds=0).run_graph(
            {"start": "root", "nodes": quorum_nodes, "edges": quorum_edges}
        )
        assert quorum_run["state"] == "succeeded"
        assert quorum_jobs.prompts == ["root", "left", "right", "done", "third"]
        quorum_state = quorum_run["counters"]["join_states"]["quorum"]
        assert quorum_state["completed_upstreams"] == ["left", "right", "third"]
        assert quorum_run["counters"]["node_counts"]["done"] == 1

        impossible_jobs = SelectiveFailJobService(db, worker["id"], {"left"})
        impossible_graph = {
            "start": "root",
            "nodes": [dict(node, quorum=3) if node["id"] == "quorum" else node for node in quorum_nodes],
            "edges": [edge for index, edge in enumerate(quorum_edges) if index != 4],
        }
        impossible = WorkflowRunner(db, impossible_jobs, poll_interval_seconds=0).run_graph(
            impossible_graph, {"stop_on_first_failure": False}
        )
        assert impossible["state"] == "failed" and "done" not in impossible_jobs.prompts
        assert impossible["counters"]["join_states"]["quorum"]["state"] == "failed"
        assert any(event["event_type"] == "join_quorum_impossible" for event in db.list_workflow_events(impossible["id"]))

        oversized = {
            "start": "root",
            "nodes": [dict(node, quorum=4) if node["id"] == "quorum" else node for node in quorum_nodes],
            "edges": [edge for index, edge in enumerate(quorum_edges) if index != 4],
        }
        assert_raises("quorum exceeds distinct incoming", validate_workflow_graph, oversized, {})


def check_role_routing() -> None:
    with TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "atlas.sqlite")
        db.upsert_worker({"name": "Reporter", "base_url": "http://127.0.0.1:1", "role": "reporter"})
        anchor = db.upsert_worker({"name": "Anchor", "base_url": "http://127.0.0.1:2", "role": "anchor"})
        decision = Router(db).resolve({"prompt": "write this", "role": "anchor"})
        assert decision.worker["id"] == anchor["id"]
        assert_raises("No routeable worker", Router(db).resolve, {"prompt": "write this", "role": "missing"})


def check_condition_runner() -> None:
    with TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "atlas.sqlite")
        worker = db.upsert_worker({"name": "Fake", "base_url": "http://127.0.0.1:1"})
        graph = {
            "start": "reporter",
            "nodes": [
                {"id": "reporter", "type": "worker", "worker_id": worker["id"], "prompt": "Report {input.topic}", "outputs": ["notes"]},
                {
                    "id": "fact_checker",
                    "type": "worker",
                    "worker_id": worker["id"],
                    "prompt": "Check {artifact.notes}",
                    "outputs": ["check"],
                    "output_format": "json",
                },
                {"id": "anchor", "type": "worker", "worker_id": worker["id"], "prompt": "Anchor {artifact.check.verdict}", "outputs": ["script"]},
            ],
            "edges": [
                {"from": "reporter", "to": "fact_checker", "condition": {"type": "always"}},
                {"from": "fact_checker", "to": "anchor", "condition": {"type": "artifact_equals", "artifact": "check", "path": "verdict", "value": "approved"}},
                {"from": "fact_checker", "to": "reporter", "condition": {"type": "artifact_in", "artifact": "check", "path": "verdict", "values": ["needs_more_sources"]}},
            ],
        }
        approved_jobs = FakeJobService(db, worker["id"], lambda prompt: '{"verdict":"approved"}' if prompt.startswith("Check ") else "ok")
        definition = db.create_workflow_definition({"name": "Approved", "graph": graph, "policy": {"max_iterations": 10}})
        approved = WorkflowRunner(db, approved_jobs, poll_interval_seconds=0).run_workflow(definition["id"], {"topic": "weather"})
        assert approved["state"] == "succeeded"
        assert [edge["to_node"] for edge in db.list_workflow_edges(approved["id"])] == ["fact_checker", "anchor"]
        assert "condition_skipped" in {event["event_type"] for event in db.list_workflow_events(approved["id"])}

    with TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "atlas.sqlite")
        worker = db.upsert_worker({"name": "Fake", "base_url": "http://127.0.0.1:1"})
        graph["nodes"][0]["worker_id"] = worker["id"]
        graph["nodes"][1]["worker_id"] = worker["id"]
        graph["nodes"][2]["worker_id"] = worker["id"]
        definition = db.create_workflow_definition({"name": "Needs more", "graph": graph, "policy": {"max_iterations": 4}})
        more_jobs = FakeJobService(db, worker["id"], lambda prompt: '{"verdict":"needs_more_sources"}' if prompt.startswith("Check ") else "ok")
        needs_more = WorkflowRunner(db, more_jobs, poll_interval_seconds=0).run_workflow(definition["id"], {"topic": "weather"})
        assert needs_more["state"] == "failed"
        assert any(edge["to_node"] == "reporter" for edge in db.list_workflow_edges(needs_more["id"]))
        assert "max_iterations exceeded" in needs_more["error"]

    with TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "atlas.sqlite")
        worker = db.upsert_worker({"name": "Fake", "base_url": "http://127.0.0.1:1"})
        guarded = dict(
            graph,
            nodes=[
                dict(graph["nodes"][0], worker_id=worker["id"]),
                dict(graph["nodes"][1], worker_id=worker["id"]),
                dict(graph["nodes"][2], worker_id=worker["id"]),
            ],
            edges=[
                graph["edges"][0],
                {"from": "fact_checker", "to": "reporter", "condition": {"type": "max_iterations_below", "node": "reporter", "max": 2}},
            ],
        )
        definition = db.create_workflow_definition({"name": "Guarded loop", "graph": guarded, "policy": {}})
        guard_jobs = FakeJobService(db, worker["id"], lambda prompt: '{"verdict":"needs_more_sources"}' if prompt.startswith("Check ") else "ok")
        guarded_run = WorkflowRunner(db, guard_jobs, poll_interval_seconds=0).run_workflow(definition["id"], {"topic": "weather"})
        assert guarded_run["state"] == "succeeded"
        assert guarded_run["counters"]["node_counts"]["reporter"] == 2
        assert "guard_tripped" in {event["event_type"] for event in db.list_workflow_events(guarded_run["id"])}


def check_managers() -> None:
    def proposal(node: str, artifacts: list[str] | None = None, instructions: str = "do it", duplicate: bool = False) -> str:
        action = {"node": node, "input_artifacts": artifacts or [], "instructions": instructions}
        return json.dumps({"stop": False, "reason": f"select {node}", "next": [action, action] if duplicate else [action]})

    def manager_graph(manager_id: str, targets: list[dict], edges: list[dict], start: str = "manager") -> dict:
        return {
            "start": start,
            "nodes": [{"id": "manager", "type": "manager", "worker_id": manager_id, "schema": "manager_decision_v1", "prompt": "Choose."}, *targets],
            "edges": edges,
        }

    def selected_edge(target: str) -> dict:
        return {"from": "manager", "to": target, "condition": {"type": "manager_selected", "target": target}}

    with TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "atlas.sqlite")
        manager_worker = db.upsert_worker({"name": "Manager", "base_url": "http://127.0.0.1:1"})
        allowed = db.upsert_worker({"name": "Allowed", "base_url": "http://127.0.0.1:2"})
        forbidden = db.upsert_worker({"name": "Forbidden", "base_url": "http://127.0.0.1:3"})
        manager_workspace = db.upsert_workspace(
            {"worker_id": manager_worker["id"], "workspace_key": "manager", "workspace_dir": "/tmp/manager"}
        )
        forbidden_workspace = db.upsert_workspace(
            {"worker_id": allowed["id"], "workspace_key": "forbidden", "workspace_dir": "/tmp/forbidden"}
        )
        workers = [manager_worker["id"], allowed["id"], forbidden["id"]]

        valid_graph = manager_graph(
            manager_worker["id"],
            [
                {"id": "seed", "type": "worker", "worker_id": allowed["id"], "prompt": "seed", "outputs": ["notes"]},
                {"id": "work", "type": "worker", "worker_id": allowed["id"], "prompt": "work"},
            ],
            [{"from": "seed", "to": "manager"}, selected_edge("work")],
            start="seed",
        )
        jobs = FakeJobService(
            db,
            manager_worker["id"],
            lambda prompt: proposal("work", ["notes"]) if "Manager context JSON:" in prompt else f"result: {prompt}",
        )
        valid = WorkflowRunner(db, jobs, poll_interval_seconds=0).run_graph(
            valid_graph,
            {"allowed_worker_ids": workers, "max_jobs": 5, "max_iterations": 5},
        )
        assert valid["state"] == "succeeded"
        assert len(jobs.prompts) == 3 and jobs.prompts[-1] == "do it\n\nwork"
        assert [edge["to_node"] for edge in db.list_workflow_edges(valid["id"])] == ["manager", "work"]
        context = json.loads(jobs.prompts[1].split("Manager context JSON:\n", 1)[1])
        assert set(context) == {"graph", "current_node", "artifacts", "counters", "policy"}
        assert context["artifacts"]["notes"] == "result: seed"
        assert valid["counters"]["manager_decisions"][0]["state"] == "accepted"
        assert any(event["event_type"] == "manager_proposal_accepted" for event in db.list_workflow_events(valid["id"]))
        assert any(entry["action"] == "workflow.manager_proposal_accepted" for entry in db.list_audit())

        duplicate_jobs = FakeJobService(
            db,
            manager_worker["id"],
            lambda prompt: proposal("work", duplicate=True) if "Manager context JSON:" in prompt else "done",
        )
        duplicate = WorkflowRunner(db, duplicate_jobs, poll_interval_seconds=0).run_graph(
            manager_graph(
                manager_worker["id"],
                [{"id": "work", "type": "worker", "worker_id": allowed["id"], "prompt": "work"}],
                [selected_edge("work")],
            )
        )
        assert duplicate["state"] == "succeeded" and len(duplicate_jobs.prompts) == 2
        assert len(db.list_workflow_edges(duplicate["id"])) == 1
        assert duplicate["counters"]["manager_decisions"][0]["items"][1]["reason"] == "duplicate target ignored"

        invalid_jobs = FakeJobService(db, manager_worker["id"], lambda _prompt: "not JSON")
        invalid = WorkflowRunner(db, invalid_jobs, poll_interval_seconds=0).run_graph(
            manager_graph(manager_worker["id"], [], [])
        )
        assert invalid["state"] == "failed" and "invalid JSON" in invalid["error"]
        assert len(invalid_jobs.prompts) == 1
        assert any(event["event_type"] == "manager_proposal_rejected" for event in db.list_workflow_events(invalid["id"]))

        no_edge_jobs = FakeJobService(db, manager_worker["id"], lambda _prompt: proposal("other"))
        no_edge = WorkflowRunner(db, no_edge_jobs, poll_interval_seconds=0).run_graph(
            manager_graph(
                manager_worker["id"],
                [
                    {"id": "allowed", "type": "worker", "worker_id": allowed["id"], "prompt": "allowed"},
                    {"id": "other", "type": "worker", "worker_id": allowed["id"], "prompt": "other"},
                ],
                [selected_edge("allowed")],
            )
        )
        assert no_edge["state"] == "failed" and "no outgoing edge" in no_edge["error"]
        assert len(no_edge_jobs.prompts) == 1

        missing_target_jobs = FakeJobService(db, manager_worker["id"], lambda _prompt: proposal("missing"))
        missing_target = WorkflowRunner(db, missing_target_jobs, poll_interval_seconds=0).run_graph(
            manager_graph(manager_worker["id"], [], [])
        )
        assert missing_target["state"] == "failed" and "target node does not exist" in missing_target["error"]
        assert len(missing_target_jobs.prompts) == 1

        forbidden_jobs = FakeJobService(db, manager_worker["id"], lambda _prompt: proposal("forbidden"))
        forbidden_run = WorkflowRunner(db, forbidden_jobs, poll_interval_seconds=0).run_graph(
            manager_graph(
                manager_worker["id"],
                [{"id": "forbidden", "type": "worker", "worker_id": forbidden["id"], "prompt": "forbidden"}],
                [selected_edge("forbidden")],
            ),
            {"allowed_worker_ids": [manager_worker["id"]]},
        )
        assert forbidden_run["state"] == "failed" and "not allowed by policy" in forbidden_run["error"]
        assert len(forbidden_jobs.prompts) == 1

        workspace_jobs = FakeJobService(db, manager_worker["id"], lambda _prompt: proposal("workspace"))
        workspace_run = WorkflowRunner(db, workspace_jobs, poll_interval_seconds=0).run_graph(
            manager_graph(
                manager_worker["id"],
                [
                    {
                        "id": "workspace",
                        "type": "worker",
                        "worker_id": allowed["id"],
                        "workspace_id": forbidden_workspace["id"],
                        "prompt": "workspace",
                    }
                ],
                [selected_edge("workspace")],
            ),
            {"allowed_worker_ids": workers, "allowed_workspace_ids": [manager_workspace["id"]]},
        )
        assert workspace_run["state"] == "failed" and "Workspace is not allowed by policy" in workspace_run["error"]
        assert len(workspace_jobs.prompts) == 1

        artifact_jobs = FakeJobService(db, manager_worker["id"], lambda _prompt: proposal("work", ["missing"]))
        artifact_run = WorkflowRunner(db, artifact_jobs, poll_interval_seconds=0).run_graph(
            manager_graph(
                manager_worker["id"],
                [{"id": "work", "type": "worker", "worker_id": allowed["id"], "prompt": "work"}],
                [selected_edge("work")],
            )
        )
        assert artifact_run["state"] == "failed" and "required artifacts are missing" in artifact_run["error"]
        assert len(artifact_jobs.prompts) == 1

        guard_jobs = FakeJobService(db, manager_worker["id"], lambda _prompt: proposal("work"))
        guarded = WorkflowRunner(db, guard_jobs, poll_interval_seconds=0).run_graph(
            manager_graph(
                manager_worker["id"],
                [{"id": "work", "type": "worker", "worker_id": allowed["id"], "prompt": "work"}],
                [selected_edge("work"), {"from": "work", "to": "manager"}],
            ),
            {"max_iterations": 1},
        )
        assert guarded["state"] == "failed" and "max_iterations exceeded" in guarded["error"]
        assert len(guard_jobs.prompts) == 1
        guard_events = {event["event_type"] for event in db.list_workflow_events(guarded["id"])}
        assert {"manager_proposal_rejected", "guard_tripped"} <= guard_events


def check_hardening() -> None:
    graph = {
        "start": "first",
        "nodes": [
            {"id": "first", "type": "worker", "prompt": "first", "outputs": ["first"]},
            {"id": "second", "type": "worker", "prompt": "second", "outputs": ["second"]},
        ],
        "edges": [{"from": "first", "to": "second", "condition": {"type": "always"}}],
    }

    with TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "atlas.sqlite")
        worker = db.upsert_worker({"name": "Fake", "base_url": "http://127.0.0.1:1"})
        definition = db.create_workflow_definition({"name": "Pause", "graph": graph})
        jobs = BlockingFakeJobService(db, worker["id"])
        runner = WorkflowRunner(db, jobs, poll_interval_seconds=0)
        run = runner.start_workflow(definition["id"])
        assert jobs.started.wait(2)
        assert runner.pause_run(run["id"])["state"] == "paused"
        jobs.release.set()
        paused = wait_for_current_nodes(db, run["id"], ["second"])
        assert paused["current_nodes"] == ["second"]
        assert jobs.prompts == ["first"]
        runner.resume_run(run["id"])
        assert wait_for_run(db, run["id"], "succeeded")["state"] == "succeeded"
        wait_for_runner_stopped(runner, run["id"])
        assert jobs.prompts == ["first", "second"]

    with TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "atlas.sqlite")
        worker = db.upsert_worker({"name": "Fake", "base_url": "http://127.0.0.1:1"})
        definition = db.create_workflow_definition({"name": "Cancel", "graph": graph})
        jobs = BlockingFakeJobService(db, worker["id"])
        runner = WorkflowRunner(db, jobs, poll_interval_seconds=0)
        run = runner.start_workflow(definition["id"])
        assert jobs.started.wait(2)
        assert runner.cancel_run(run["id"])["state"] == "cancelled"
        jobs.release.set()
        jobs.wait_stopped()
        wait_for_runner_stopped(runner, run["id"])
        assert db.get_workflow_run(run["id"])["state"] == "cancelled"
        assert jobs.prompts == ["first"]

    with TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "atlas.sqlite")
        worker = db.upsert_worker({"name": "Fake", "base_url": "http://127.0.0.1:1"})
        jobs = FakeJobService(db, worker["id"])
        definition = db.create_workflow_definition({"name": "Expired", "graph": graph, "policy": {"max_minutes": 1}})
        expired = db.create_workflow_run(
            {
                "workflow_definition_id": definition["id"],
                "name": "Expired",
                "state": "paused",
                "current_nodes": ["first"],
                "started_at": "2000-01-01T00:00:00Z",
            }
        )
        runner = WorkflowRunner(db, jobs, poll_interval_seconds=0)
        runner.resume_run(expired["id"])
        expired = wait_for_run(db, expired["id"], "failed")
        wait_for_runner_stopped(runner, expired["id"])
        assert expired["state"] == "failed"
        assert "max_minutes exceeded" in expired["error"]
        assert not jobs.prompts

    with TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "atlas.sqlite")
        allowed = db.upsert_worker({"name": "Allowed", "base_url": "http://127.0.0.1:1"})
        denied = db.upsert_worker({"name": "Denied", "base_url": "http://127.0.0.1:2"})
        jobs = FakeJobService(db, allowed["id"])
        denied_graph = {
            "start": "only",
            "nodes": [{"id": "only", "type": "worker", "worker_id": denied["id"], "prompt": "no"}],
            "edges": [],
        }
        run = WorkflowRunner(db, jobs, poll_interval_seconds=0).run_graph(
            denied_graph,
            {"allowed_worker_ids": [allowed["id"]]},
        )
        assert run["state"] == "failed"
        assert "not allowed by policy" in run["error"]
        assert not jobs.prompts
        assert not db.list_jobs()


def check_budget_and_failure_policy() -> None:
    with TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "atlas.sqlite")
        worker = db.upsert_worker({"name": "Fake", "base_url": "http://127.0.0.1:1"})
        graph = {
            "start": "one",
            "nodes": [
                {"id": "one", "type": "worker", "worker_id": worker["id"], "prompt": "one", "budget_units": 1},
                {"id": "two", "type": "worker", "worker_id": worker["id"], "prompt": "two", "budget_units": 2},
                {"id": "three", "type": "worker", "worker_id": worker["id"], "prompt": "three"},
            ],
            "edges": [{"from": "one", "to": "two"}, {"from": "two", "to": "three"}],
        }
        jobs = FakeJobService(db, worker["id"])
        run = WorkflowRunner(db, jobs, poll_interval_seconds=0).run_graph(graph, {"max_budget_units": 3})
        assert run["state"] == "failed" and "max_budget_units exceeded" in run["error"]
        assert jobs.prompts == ["one", "two"] and run["counters"]["budget_units_spent"] == 3

        failed_jobs = SelectiveFailJobService(db, worker["id"], {"one"})
        failed = WorkflowRunner(db, failed_jobs, poll_interval_seconds=0).run_graph(
            {"start": "one", "nodes": [graph["nodes"][0]], "edges": []}, {"max_budget_units": 2}
        )
        assert failed["state"] == "failed" and failed["counters"]["budget_units_spent"] == 1

        fanout = {
            "start": "root",
            "nodes": [
                {"id": "root", "type": "worker", "worker_id": worker["id"], "prompt": "root"},
                {"id": "left", "type": "worker", "worker_id": worker["id"], "prompt": "left"},
                {"id": "right", "type": "worker", "worker_id": worker["id"], "prompt": "right"},
                {"id": "from_left", "type": "worker", "worker_id": worker["id"], "prompt": "must not run"},
            ],
            "edges": [
                {"from": "root", "to": "left"},
                {"from": "root", "to": "right"},
                {"from": "left", "to": "from_left"},
            ],
        }
        stop_jobs = SelectiveFailJobService(db, worker["id"], {"left"})
        stopped = WorkflowRunner(db, stop_jobs, poll_interval_seconds=0).run_graph(fanout)
        assert stopped["state"] == "failed" and stop_jobs.prompts == ["root", "left"]

        continue_jobs = SelectiveFailJobService(db, worker["id"], {"left"})
        continued = WorkflowRunner(db, continue_jobs, poll_interval_seconds=0).run_graph(
            fanout, {"stop_on_first_failure": False}
        )
        assert continued["state"] == "failed" and continue_jobs.prompts == ["root", "left", "right"]
        assert continued["counters"]["failure_summary"] == {"count": 1, "nodes": ["left"]}
        assert not any(edge["from_node"] == "left" for edge in db.list_workflow_edges(continued["id"]))

        manager = {"id": "manager", "type": "manager", "worker_id": worker["id"], "schema": "manager_decision_v1", "budget_units": 1}
        targets = [
            {"id": "a", "type": "worker", "worker_id": worker["id"], "prompt": "a", "budget_units": 2},
            {"id": "b", "type": "worker", "worker_id": worker["id"], "prompt": "b", "budget_units": 2},
        ]
        decision = json.dumps({
            "stop": False,
            "reason": "both",
            "next": [
                {"node": "a", "input_artifacts": [], "instructions": ""},
                {"node": "b", "input_artifacts": [], "instructions": ""},
            ],
        })
        manager_jobs = FakeJobService(db, worker["id"], lambda prompt: decision if "Manager context JSON:" in prompt else "ok")
        manager_run = WorkflowRunner(db, manager_jobs, poll_interval_seconds=0).run_graph(
            {
                "start": "manager",
                "nodes": [manager, *targets],
                "edges": [
                    {"from": "manager", "to": "a", "condition": {"type": "manager_selected", "target": "a"}},
                    {"from": "manager", "to": "b", "condition": {"type": "manager_selected", "target": "b"}},
                ],
            },
            {"max_budget_units": 4},
        )
        assert manager_run["state"] == "failed" and "max_budget_units exceeded" in manager_run["error"]
        assert manager_jobs.prompts == [manager_jobs.prompts[0]]


def check_human_gates() -> None:
    with TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "atlas.sqlite")
        worker = db.upsert_worker({"name": "Fake", "base_url": "http://127.0.0.1:1"})
        graph = {
            "start": "gate",
            "nodes": [
                {"id": "gate", "type": "human_gate", "label": "Approve publish"},
                {"id": "publish", "type": "worker", "worker_id": worker["id"], "prompt": "publish"},
            ],
            "edges": [{"from": "gate", "to": "publish"}],
        }
        definition = db.create_workflow_definition({"name": "Approval", "graph": graph})
        jobs = FakeJobService(db, worker["id"])
        runner = WorkflowRunner(db, jobs, poll_interval_seconds=0)

        waiting = runner.run_workflow(definition["id"])
        assert waiting["state"] == "waiting_for_human"
        assert waiting["current_nodes"] == []
        assert not jobs.prompts and not db.list_jobs()
        approval = db.list_approvals(state="pending", run_id=waiting["id"])[0]
        gate_node = db.list_workflow_nodes(waiting["id"])[0]
        assert gate_node["state"] == "waiting_for_human" and gate_node["job_id"] is None

        runner.approve_approval(approval["id"])
        approved = wait_for_run(db, waiting["id"], "succeeded")
        wait_for_runner_stopped(runner, waiting["id"])
        assert jobs.prompts == ["publish"]
        assert approved["counters"]["completed_nodes"] == ["gate", "publish"]
        assert db.get_approval(approval["id"])["state"] == "approved"
        assert_raises("already approved", runner.approve_approval, approval["id"])
        assert jobs.prompts == ["publish"]

        rejected_run = runner.run_workflow(definition["id"])
        rejected_approval = db.list_approvals(state="pending", run_id=rejected_run["id"])[0]
        rejected = runner.reject_approval(rejected_approval["id"])["run"]
        assert rejected["state"] == "failed"
        assert "human approval rejected" in rejected["error"]
        assert jobs.prompts == ["publish"]
        assert_raises("already rejected", runner.reject_approval, rejected_approval["id"])
        event_types = {event["event_type"] for event in db.list_workflow_events(rejected_run["id"])}
        assert {"approval_created", "approval_rejected", "node_failed", "run_finished"} <= event_types

    with TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "atlas.sqlite")
        worker = db.upsert_worker({"name": "Fake", "base_url": "http://127.0.0.1:1"})
        graph = {
            "start": "gate",
            "nodes": [
                {
                    "id": "gate",
                    "type": "human_gate",
                    "label": "Choose branch",
                    "choices": [{"id": "left", "label": "Left"}, {"id": "right", "label": "Right"}],
                },
                {"id": "left", "type": "worker", "worker_id": worker["id"], "prompt": "left"},
                {"id": "right", "type": "worker", "worker_id": worker["id"], "prompt": "right"},
            ],
            "edges": [
                {"from": "gate", "to": "left", "condition": {"type": "human_selected", "choice": "left"}},
                {"from": "gate", "to": "right", "condition": {"type": "human_selected", "choice": "right"}},
            ],
        }
        definition = db.create_workflow_definition({"name": "Choice", "graph": graph})
        jobs = FakeJobService(db, worker["id"])
        runner = WorkflowRunner(db, jobs, poll_interval_seconds=0)
        waiting = runner.run_workflow(definition["id"])
        approval = db.list_approvals(state="pending", run_id=waiting["id"])[0]
        assert approval["choices"][0]["id"] == "left" and not db.list_workflow_edges(waiting["id"])
        runner.choose_approval(approval["id"], "right")
        chosen = wait_for_run(db, waiting["id"], "succeeded")
        wait_for_runner_stopped(runner, waiting["id"])
        assert jobs.prompts == ["right"]
        assert db.get_approval(approval["id"])["selected_choice"] == "right"
        assert [edge["to_node"] for edge in db.list_workflow_edges(chosen["id"])] == ["right"]
        assert_raises("already chosen", runner.choose_approval, approval["id"], "right")

    with TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "atlas.sqlite")
        worker = db.upsert_worker({"name": "Fake", "base_url": "http://127.0.0.1:1"})
        graph = {
            "start": "loop",
            "nodes": [{"id": "loop", "type": "worker", "worker_id": worker["id"], "prompt": "loop"}],
            "edges": [{"from": "loop", "to": "loop", "condition": {"type": "max_iterations_below", "node": "loop", "max": 3}}],
        }
        definition = db.create_workflow_definition(
            {"name": "Guarded approval loop", "graph": graph, "policy": {"requires_human_after_iterations": 2}}
        )
        jobs = FakeJobService(db, worker["id"])
        runner = WorkflowRunner(db, jobs, poll_interval_seconds=0)
        waiting = runner.run_workflow(definition["id"])
        assert waiting["state"] == "waiting_for_human"
        assert jobs.prompts == ["loop", "loop"]
        approvals = db.list_approvals(state="pending", run_id=waiting["id"])
        assert len(approvals) == 1 and approvals[0]["approval_key"] == "policy:requires_human_after_iterations"
        runner.approve_approval(approvals[0]["id"])
        resumed = wait_for_run(db, waiting["id"], "succeeded")
        wait_for_runner_stopped(runner, waiting["id"])
        assert jobs.prompts == ["loop", "loop", "loop"]
        assert resumed["counters"]["node_counts"]["loop"] == 3
        assert len(db.list_approvals(run_id=waiting["id"])) == 1


def check_recovery() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "atlas.sqlite"
        db = Database(path)
        worker = db.upsert_worker({"name": "Fake", "base_url": "http://127.0.0.1:1"})
        graph = {
            "start": "first",
            "nodes": [
                {"id": "first", "type": "worker", "worker_id": worker["id"], "prompt": "first"},
                {"id": "second", "type": "worker", "worker_id": worker["id"], "prompt": "second"},
            ],
            "edges": [{"from": "first", "to": "second"}],
        }
        definition = db.create_workflow_definition({"name": "Recovery", "graph": graph})
        run = db.create_workflow_run(
            {
                "workflow_definition_id": definition["id"],
                "state": "running",
                "current_nodes": ["second"],
                "counters": {
                    "jobs_started": 1,
                    "budget_units_spent": 1,
                    "node_counts": {"first": 1, "second": 1},
                    "completed_nodes": ["first"],
                    "failed_nodes": [],
                },
                "started_at": now_iso(),
            }
        )
        db.create_workflow_node({"run_id": run["id"], "node_key": "first", "state": "succeeded", "attempt": 1})
        old_job = db.create_job({"worker_id": worker["id"], "prompt": "second", "state": "running"})
        interrupted_node = db.create_workflow_node(
            {"run_id": run["id"], "node_key": "second", "state": "running", "attempt": 1, "job_id": old_job["id"]}
        )

        reopened = Database(path)
        jobs = FakeJobService(reopened, worker["id"])
        runner = WorkflowRunner(reopened, jobs, poll_interval_seconds=0)
        runner.reconcile_runs()
        recovered = reopened.get_workflow_run(run["id"])
        assert recovered["state"] == "recovery_required" and len(reopened.list_jobs()) == 1
        assert reopened.get_workflow_node(interrupted_node["id"])["state"] == "interrupted"
        assert recovered["counters"]["recovery"]["interrupted"][0]["job_id"] == old_job["id"]
        assert_raises("retry_interrupted authorization", runner.resume_run, run["id"])
        runner.resume_run(run["id"], retry_interrupted=True)
        retried = wait_for_run(reopened, run["id"], "succeeded")
        wait_for_runner_stopped(runner, run["id"])
        assert jobs.prompts == ["second"] and retried["counters"]["completed_nodes"] == ["first", "second"]
        assert len(reopened.list_jobs()) == 2
        event_types = [event["event_type"] for event in reopened.list_workflow_events(run["id"])]
        assert event_types.index("recovery_required") < event_types.index("recovery_retry_authorized")
        assert any(entry["action"] == "workflow.recovery_required" for entry in reopened.list_audit())

        control_definition = reopened.create_workflow_definition(
            {"name": "Control recovery", "graph": {"start": "done", "nodes": [{"id": "done", "type": "join", "mode": "all"}], "edges": []}}
        )
        control_run = reopened.create_workflow_run(
            {"workflow_definition_id": control_definition["id"], "state": "running", "current_nodes": ["done"], "started_at": now_iso()}
        )
        runner.reconcile_runs()
        assert wait_for_run(reopened, control_run["id"], "succeeded")["state"] == "succeeded"
        wait_for_runner_stopped(runner, control_run["id"])

        gate_definition = reopened.create_workflow_definition(
            {
                "name": "Pending gate",
                "graph": {
                    "start": "gate",
                    "nodes": [{"id": "gate", "type": "human_gate"}, {"id": "done", "type": "join", "mode": "all"}],
                    "edges": [{"from": "gate", "to": "done"}],
                },
            }
        )
        waiting = runner.run_workflow(gate_definition["id"])
        approval = reopened.list_approvals(state="pending", run_id=waiting["id"])[0]
        runner.reconcile_runs()
        assert reopened.get_workflow_run(waiting["id"])["state"] == "waiting_for_human"
        runner.approve_approval(approval["id"])
        assert wait_for_run(reopened, waiting["id"], "succeeded")["state"] == "succeeded"
        wait_for_runner_stopped(runner, waiting["id"])


class FakeJobService:
    def __init__(self, db: Database, worker_id: str, responder=None):
        self.db = db
        self.worker_id = worker_id
        self.responder = responder
        self.prompts: list[str] = []
        self.payloads: list[dict] = []

    def submit(self, payload: dict) -> dict:
        prompt = payload["prompt"]
        self.payloads.append(dict(payload))
        self.prompts.append(prompt)
        job = self.db.create_job({"worker_id": self.worker_id, "prompt": prompt, "state": "running"})
        self.db.append_job_text(job["id"], self.responder(prompt) if self.responder else f"result: {prompt}")
        self.db.update_job(job["id"], state="succeeded", finished_at=now_iso())
        return self.db.get_job(job["id"]) or job


class SelectiveFailJobService(FakeJobService):
    def __init__(self, db: Database, worker_id: str, failing_prompts: set[str]):
        super().__init__(db, worker_id)
        self.failing_prompts = failing_prompts

    def submit(self, payload: dict) -> dict:
        prompt = payload["prompt"]
        self.payloads.append(dict(payload))
        self.prompts.append(prompt)
        state = "failed" if prompt in self.failing_prompts else "succeeded"
        job = self.db.create_job({"worker_id": self.worker_id, "prompt": prompt, "state": state})
        if state == "succeeded":
            self.db.append_job_text(job["id"], f"result: {prompt}")
        else:
            self.db.update_job(job["id"], error=f"failed: {prompt}")
        self.db.update_job(job["id"], state=state, finished_at=now_iso())
        return self.db.get_job(job["id"]) or job


class BlockingFakeJobService(FakeJobService):
    def __init__(self, db: Database, worker_id: str):
        super().__init__(db, worker_id)
        self.started = threading.Event()
        self.release = threading.Event()
        self.threads: list[threading.Thread] = []

    def submit(self, payload: dict) -> dict:
        prompt = payload["prompt"]
        self.payloads.append(dict(payload))
        self.prompts.append(prompt)
        job = self.db.create_job({"worker_id": self.worker_id, "prompt": prompt, "state": "running"})
        if len(self.prompts) == 1:
            self.started.set()
            thread = threading.Thread(target=self._finish_after_release, args=(job["id"], prompt), daemon=True)
            self.threads.append(thread)
            thread.start()
        else:
            self._finish(job["id"], prompt)
        return self.db.get_job(job["id"]) or job

    def _finish_after_release(self, job_id: str, prompt: str) -> None:
        if self.release.wait(2):
            self._finish(job_id, prompt)

    def _finish(self, job_id: str, prompt: str) -> None:
        self.db.append_job_text(job_id, f"result: {prompt}")
        self.db.update_job(job_id, state="succeeded", finished_at=now_iso())

    def wait_stopped(self) -> None:
        for thread in self.threads:
            thread.join(timeout=2)
            assert not thread.is_alive()


def wait_for_run(db: Database, run_id: str, state: str) -> dict:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        run = db.get_workflow_run(run_id)
        if run and run["state"] == state:
            return run
        time.sleep(0.01)
    raise AssertionError(f"workflow run did not reach {state}: {db.get_workflow_run(run_id)}")


def wait_for_current_nodes(db: Database, run_id: str, current_nodes: list[str]) -> dict:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        run = db.get_workflow_run(run_id)
        if run and run["current_nodes"] == current_nodes:
            return run
        time.sleep(0.01)
    raise AssertionError(f"workflow run did not stop before {current_nodes}: {db.get_workflow_run(run_id)}")


def wait_for_runner_stopped(runner: WorkflowRunner, run_id: str) -> None:
    """`_run_background`'s finally block pops `run_id` from `_threads` and THEN does a DB read
    (and maybe a respawn), all under `_thread_lock` — so an unsynchronized `run_id not in
    _threads` check can observe the pop before that DB read finishes, letting a caller tear
    down its TemporaryDirectory out from under a still-running background thread (the
    "Directory not empty" / "disk I/O error" flake). Acquiring the same lock here makes this a
    real barrier: we can only see the dict update once the whole critical section — pop, DB
    read, and any respawn — has released it."""
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with runner._thread_lock:
            if run_id not in runner._threads:
                return
        time.sleep(0.01)
    raise AssertionError(f"workflow runner did not stop: {run_id}")


def assert_raises(message: str, func, *args, **kwargs) -> None:
    try:
        func(*args, **kwargs)
    except ValueError as exc:
        assert message in str(exc), str(exc)
        return
    raise AssertionError(f"expected ValueError containing: {message}")


if __name__ == "__main__":
    main()
