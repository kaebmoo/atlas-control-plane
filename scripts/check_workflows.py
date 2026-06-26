from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.db import Database, now_iso
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
    check_runner()
    check_condition_runner()
    print("workflow validation/render check ok")


def check_runner() -> None:
    with TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "atlas.sqlite")
        worker = db.upsert_worker({"name": "Fake", "base_url": "http://127.0.0.1:1"})
        graph = {
            "start": "reporter",
            "nodes": [
                {"id": "reporter", "type": "worker", "worker_id": worker["id"], "prompt": "Topic: {input.topic}", "outputs": ["notes"]},
                {"id": "anchor", "type": "worker", "worker_id": worker["id"], "prompt": "Read: {artifact.notes}", "outputs": ["script"]},
            ],
            "edges": [{"from": "reporter", "to": "anchor", "condition": {"type": "always"}}],
        }
        definition = db.create_workflow_definition({"name": "Linear", "graph": graph, "policy": {"max_jobs": 5}})
        fake_jobs = FakeJobService(db, worker["id"])
        run = WorkflowRunner(db, fake_jobs, poll_interval_seconds=0).run_workflow(definition["id"], {"topic": "weather"})

        assert run["state"] == "succeeded"
        assert run["current_nodes"] == []
        assert fake_jobs.prompts == ["Topic: weather", "Read: result: Topic: weather"]
        assert [node["state"] for node in db.list_workflow_nodes(run["id"])] == ["succeeded", "succeeded"]
        assert [edge["to_node"] for edge in db.list_workflow_edges(run["id"])] == ["anchor"]
        assert {artifact["key"]: artifact["content"] for artifact in db.list_artifacts(run_id=run["id"])} == {
            "notes": "result: Topic: weather",
            "script": "result: Read: result: Topic: weather",
        }


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


class FakeJobService:
    def __init__(self, db: Database, worker_id: str, responder=None):
        self.db = db
        self.worker_id = worker_id
        self.responder = responder
        self.prompts: list[str] = []

    def submit(self, payload: dict) -> dict:
        prompt = payload["prompt"]
        self.prompts.append(prompt)
        job = self.db.create_job({"worker_id": self.worker_id, "prompt": prompt, "state": "running"})
        self.db.append_job_text(job["id"], self.responder(prompt) if self.responder else f"result: {prompt}")
        self.db.update_job(job["id"], state="succeeded", finished_at=now_iso())
        return self.db.get_job(job["id"]) or job


def assert_raises(message: str, func, *args, **kwargs) -> None:
    try:
        func(*args, **kwargs)
    except ValueError as exc:
        assert message in str(exc), str(exc)
        return
    raise AssertionError(f"expected ValueError containing: {message}")


if __name__ == "__main__":
    main()
