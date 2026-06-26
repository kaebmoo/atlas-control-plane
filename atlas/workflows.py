from __future__ import annotations

import json
import re
from typing import Any


_FIELD_RE = re.compile(r"{([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)}")


def validate_workflow_graph(graph: dict[str, Any], policy: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(graph, dict):
        raise ValueError("workflow graph must be an object")
    if policy is not None and not isinstance(policy, dict):
        raise ValueError("workflow policy must be an object")

    nodes = graph.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("workflow graph nodes must be a non-empty list")

    node_ids = set()
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise ValueError(f"workflow node at index {index} must be an object")
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id.strip():
            raise ValueError(f"workflow node at index {index} requires a non-empty id")
        if node_id in node_ids:
            raise ValueError(f"duplicate node id: {node_id}")
        node_ids.add(node_id)
        if not isinstance(node.get("type"), str) or not node["type"].strip():
            raise ValueError(f"workflow node {node_id} requires a non-empty type")

    start = graph.get("start")
    if not isinstance(start, str) or not start.strip():
        raise ValueError("workflow graph requires start")
    if start not in node_ids:
        raise ValueError(f"workflow graph start references missing node: {start}")

    edges = graph.get("edges", [])
    if not isinstance(edges, list):
        raise ValueError("workflow graph edges must be a list")
    for index, edge in enumerate(edges):
        _validate_edge(edge, index, node_ids)

    if _has_cycle(node_ids, edges) and not _has_loop_guard(policy or {}):
        raise ValueError("workflow graph has a cycle; policy.max_iterations is required")

    return graph


def render_prompt(
    template: str,
    input: dict[str, Any] | None = None,
    artifacts: dict[str, Any] | list[dict[str, Any]] | None = None,
    run: dict[str, Any] | None = None,
    node: dict[str, Any] | None = None,
    job: dict[str, Any] | None = None,
) -> str:
    if not isinstance(template, str):
        raise ValueError("prompt template must be a string")
    context = {
        "input": _as_dict(input, "input"),
        "artifact": _artifact_map(artifacts),
        "run": _as_dict(run, "run"),
        "node": _as_dict(node, "node"),
        "job": _as_dict(job, "job"),
    }

    def replace(match: re.Match[str]) -> str:
        return _prompt_value(_resolve_path(match.group(1), context))

    return _FIELD_RE.sub(replace, template)


def _validate_edge(edge: Any, index: int, node_ids: set[str]) -> None:
    if not isinstance(edge, dict):
        raise ValueError(f"workflow edge at index {index} must be an object")
    from_node = edge.get("from")
    to_node = edge.get("to")
    if not isinstance(from_node, str) or from_node not in node_ids:
        raise ValueError(f"workflow edge at index {index} references missing from node: {from_node}")
    if not isinstance(to_node, str) or to_node not in node_ids:
        raise ValueError(f"workflow edge at index {index} references missing to node: {to_node}")
    if edge.get("condition", {"type": "always"}) != {"type": "always"}:
        raise ValueError(f"workflow edge at index {index} uses unsupported condition; only {{\"type\":\"always\"}} is supported")


def _has_cycle(node_ids: set[str], edges: list[dict[str, Any]]) -> bool:
    outgoing = {node_id: [] for node_id in node_ids}
    for edge in edges:
        outgoing[edge["from"]].append(edge["to"])

    visiting = set()
    visited = set()

    def visit(node_id: str) -> bool:
        if node_id in visiting:
            return True
        if node_id in visited:
            return False
        visiting.add(node_id)
        for next_node in outgoing[node_id]:
            if visit(next_node):
                return True
        visiting.remove(node_id)
        visited.add(node_id)
        return False

    return any(visit(node_id) for node_id in node_ids)


def _has_loop_guard(policy: dict[str, Any]) -> bool:
    value = policy.get("max_iterations")
    return isinstance(value, int) and value > 0


def _artifact_map(artifacts: dict[str, Any] | list[dict[str, Any]] | None) -> dict[str, Any]:
    if artifacts is None:
        return {}
    if isinstance(artifacts, dict):
        return artifacts
    if isinstance(artifacts, list):
        return {str(item["key"]): item.get("content", "") for item in artifacts if isinstance(item, dict) and item.get("key")}
    raise ValueError("artifacts must be an object or a list")


def _as_dict(value: dict[str, Any] | None, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} metadata must be an object")
    return value


def _resolve_path(name: str, context: dict[str, dict[str, Any]]) -> Any:
    parts = name.split(".")
    root = parts[0]
    if root not in context:
        raise ValueError(f"unknown prompt variable: {{{name}}}")
    value: Any = context[root]
    for part in parts[1:]:
        if not isinstance(value, dict) or part not in value:
            raise ValueError(f"missing prompt variable: {{{name}}}")
        value = value[part]
    return value


def _prompt_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    return str(value)
