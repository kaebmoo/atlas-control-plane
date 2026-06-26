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

    bad_condition = dict(graph, edges=[{"from": "reporter", "to": "anchor", "condition": {"type": "artifact_equals"}}])
    assert_raises("unsupported condition", validate_workflow_graph, bad_condition, {})

    cycle = dict(graph, edges=graph["edges"] + [{"from": "anchor", "to": "reporter", "condition": {"type": "always"}}])
    assert_raises("policy.max_iterations is required", validate_workflow_graph, cycle, {})
    assert validate_workflow_graph(cycle, {"max_iterations": 2}) is cycle

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


class FakeJobService:
    def __init__(self, db: Database, worker_id: str):
        self.db = db
        self.worker_id = worker_id
        self.prompts: list[str] = []

    def submit(self, payload: dict) -> dict:
        prompt = payload["prompt"]
        self.prompts.append(prompt)
        job = self.db.create_job({"worker_id": self.worker_id, "prompt": prompt, "state": "running"})
        self.db.append_job_text(job["id"], f"result: {prompt}")
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
