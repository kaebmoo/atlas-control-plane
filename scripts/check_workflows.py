from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.db import Database, now_iso
from atlas.router import Router
from atlas.workflows import WorkflowRunner, render_prompt, validate_workflow_graph


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
    assert_raises("mode must be all or any", validate_workflow_graph, bad_join, {})

    human_gate = {"start": "gate", "nodes": [{"id": "gate", "type": "human_gate", "label": "Approve"}], "edges": []}
    assert validate_workflow_graph(human_gate, {}) is human_gate

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
    check_human_gates()
    check_hardening()
    print("workflow validation/render check ok")


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
        assert waiting["current_nodes"] == ["publish"]
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
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
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
