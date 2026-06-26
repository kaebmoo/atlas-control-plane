from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.workflows import render_prompt, validate_workflow_graph


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
    print("workflow validation/render check ok")


def assert_raises(message: str, func, *args, **kwargs) -> None:
    try:
        func(*args, **kwargs)
    except ValueError as exc:
        assert message in str(exc), str(exc)
        return
    raise AssertionError(f"expected ValueError containing: {message}")


if __name__ == "__main__":
    main()
