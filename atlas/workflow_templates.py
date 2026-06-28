from __future__ import annotations

from copy import deepcopy
from typing import Any


_TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "news_desk",
        "name": "News Desk",
        "description": "Reporter, fact checker, editor, and anchor with a bounded correction loop.",
        "graph": {
            "start": "reporter",
            "nodes": [
                {"id": "reporter", "type": "worker", "role": "reporter", "prompt": "Find facts about {input.topic}.", "outputs": ["reporter_notes"]},
                {"id": "fact_checker", "type": "worker", "role": "fact_checker", "prompt": "Return JSON with verdict approved or needs_more_sources for {artifact.reporter_notes}.", "outputs": ["fact_check"], "output_format": "json"},
                {"id": "editor", "type": "worker", "role": "editor", "prompt": "Edit these verified notes: {artifact.reporter_notes}", "outputs": ["edited_notes"]},
                {"id": "anchor", "type": "worker", "role": "anchor", "prompt": "Write a broadcast script from {artifact.edited_notes}.", "outputs": ["script"]},
            ],
            "edges": [
                {"from": "reporter", "to": "fact_checker", "condition": {"type": "always"}},
                {"from": "fact_checker", "to": "editor", "condition": {"type": "artifact_equals", "artifact": "fact_check", "path": "verdict", "value": "approved"}},
                {"from": "fact_checker", "to": "reporter", "condition": {"type": "artifact_equals", "artifact": "fact_check", "path": "verdict", "value": "needs_more_sources"}},
                {"from": "editor", "to": "anchor", "condition": {"type": "always"}},
            ],
        },
        "policy": {"max_jobs": 10, "max_iterations": 3, "max_attempts_per_node": 3, "max_budget_units": 10},
    },
    {
        "id": "research_writer_reviewer",
        "name": "Researcher -> Writer -> Reviewer",
        "description": "Research, draft, and review a topic in three deterministic steps.",
        "graph": {
            "start": "researcher",
            "nodes": [
                {"id": "researcher", "type": "worker", "role": "researcher", "prompt": "Research {input.topic}.", "outputs": ["research"]},
                {"id": "writer", "type": "worker", "role": "writer", "prompt": "Write from {artifact.research}.", "outputs": ["draft"]},
                {"id": "reviewer", "type": "worker", "role": "reviewer", "prompt": "Review {artifact.draft}.", "outputs": ["review"]},
            ],
            "edges": [
                {"from": "researcher", "to": "writer", "condition": {"type": "always"}},
                {"from": "writer", "to": "reviewer", "condition": {"type": "always"}},
            ],
        },
        "policy": {"max_jobs": 3, "max_iterations": 3, "max_attempts_per_node": 1, "max_budget_units": 3},
    },
    {
        "id": "coder_tester_reviewer",
        "name": "Coder -> Tester -> Reviewer",
        "description": "Implement, test, and review a bounded code change.",
        "graph": {
            "start": "coder",
            "nodes": [
                {"id": "coder", "type": "worker", "role": "coder", "prompt": "Implement {input.task}.", "outputs": ["implementation"]},
                {"id": "tester", "type": "worker", "role": "tester", "prompt": "Test {artifact.implementation}.", "outputs": ["test_report"]},
                {"id": "reviewer", "type": "worker", "role": "reviewer", "prompt": "Review {artifact.implementation} with {artifact.test_report}.", "outputs": ["review"]},
            ],
            "edges": [
                {"from": "coder", "to": "tester", "condition": {"type": "always"}},
                {"from": "tester", "to": "reviewer", "condition": {"type": "always"}},
            ],
        },
        "policy": {"max_jobs": 3, "max_iterations": 3, "max_attempts_per_node": 1, "max_budget_units": 3},
    },
    {
        "id": "manager_loop_max_3",
        "name": "Manager-directed loop with max 3 iterations",
        "description": "A manager may request research or a final review within a three-iteration hard limit.",
        "graph": {
            "start": "manager",
            "nodes": [
                {"id": "manager", "type": "manager", "role": "manager", "schema": "manager_decision_v1", "prompt": "Choose research, review, or stop.", "budget_units": 1},
                {"id": "researcher", "type": "worker", "role": "researcher", "prompt": "Research {input.topic}.", "outputs": ["research"]},
                {"id": "reviewer", "type": "worker", "role": "reviewer", "prompt": "Review {artifact.research}.", "outputs": ["review"]},
            ],
            "edges": [
                {"from": "manager", "to": "researcher", "condition": {"type": "manager_selected", "target": "researcher"}},
                {"from": "manager", "to": "reviewer", "condition": {"type": "manager_selected", "target": "reviewer"}},
                {"from": "researcher", "to": "manager", "condition": {"type": "always"}},
            ],
        },
        "policy": {"max_jobs": 3, "max_iterations": 3, "max_attempts_per_node": 3, "max_minutes": 30, "max_budget_units": 3},
    },
]


def workflow_templates() -> list[dict[str, Any]]:
    return deepcopy(_TEMPLATES)
