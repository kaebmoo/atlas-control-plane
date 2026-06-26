from __future__ import annotations

import json
import re
import time
from typing import Any

from .db import Database, now_iso


_FIELD_RE = re.compile(r"{([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)}")
_JOB_TERMINAL_STATES = {"succeeded", "failed", "cancelled"}


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


class WorkflowRunner:
    def __init__(self, db: Database, job_service: Any, poll_interval_seconds: float = 0.2, max_wait_seconds: float = 3600):
        self.db = db
        self.job_service = job_service
        self.poll_interval_seconds = poll_interval_seconds
        self.max_wait_seconds = max_wait_seconds

    def run_workflow(self, workflow_definition_id: str, input: dict[str, Any] | None = None) -> dict[str, Any]:
        definition = self.db.get_workflow_definition(workflow_definition_id)
        if not definition:
            raise ValueError(f"Unknown workflow_definition_id: {workflow_definition_id}")
        return self.run_graph(
            definition["graph"],
            definition.get("policy") or {},
            input=input,
            workflow_definition_id=workflow_definition_id,
            name=definition.get("name") or "Workflow run",
        )

    def run_graph(
        self,
        graph: dict[str, Any],
        policy: dict[str, Any] | None = None,
        input: dict[str, Any] | None = None,
        workflow_definition_id: str | None = None,
        name: str = "Workflow run",
    ) -> dict[str, Any]:
        policy = policy or {}
        input = input or {}
        validate_workflow_graph(graph, policy)

        node_map = {node["id"]: node for node in graph["nodes"]}
        outgoing = _outgoing_edges(graph.get("edges", []))
        ready = [graph["start"]]
        artifacts: dict[str, Any] = {}
        run = self.db.create_workflow_run(
            {
                "workflow_definition_id": workflow_definition_id,
                "name": name,
                "state": "running",
                "input": input,
                "current_nodes": ready,
                "started_at": now_iso(),
            }
        )

        try:
            jobs_started = 0
            while ready:
                self.db.update_workflow_run(run["id"], current_nodes=ready, counters={"jobs_started": jobs_started})
                next_ready: list[str] = []
                for node_key in ready:
                    _check_limit(policy, "max_jobs", jobs_started)
                    _check_limit(policy, "max_iterations", jobs_started)
                    node = node_map[node_key]
                    runtime_node = self.db.create_workflow_node(
                        {
                            "run_id": run["id"],
                            "node_key": node_key,
                            "state": "running",
                            "attempt": 1,
                            "input_artifacts": list(artifacts),
                            "started_at": now_iso(),
                        }
                    )
                    try:
                        job = self._run_worker_node(run, node, input, artifacts)
                        jobs_started += 1
                        if job["state"] != "succeeded":
                            raise ValueError(f"workflow node {node_key} job {job['id']} ended as {job['state']}")
                        output_artifacts = self._store_output_artifact(run["id"], node, job, artifacts)
                        self.db.update_workflow_node(
                            runtime_node["id"],
                            state="succeeded",
                            job_id=job["id"],
                            output_artifacts=output_artifacts,
                            finished_at=now_iso(),
                        )
                    except Exception as exc:
                        self.db.update_workflow_node(runtime_node["id"], state="failed", error=str(exc), finished_at=now_iso())
                        raise

                    for edge in outgoing.get(node_key, []):
                        self.db.append_workflow_edge(run["id"], edge["from"], edge["to"], {"type": "always", "matched": True})
                        next_ready.append(edge["to"])
                ready = next_ready

            self.db.update_workflow_run(run["id"], state="succeeded", current_nodes=[], counters={"jobs_started": jobs_started}, finished_at=now_iso())
        except Exception as exc:
            self.db.update_workflow_run(run["id"], state="failed", current_nodes=[], error=str(exc), finished_at=now_iso())
        return self.db.get_workflow_run(run["id"]) or run

    def _run_worker_node(self, run: dict[str, Any], node: dict[str, Any], input: dict[str, Any], artifacts: dict[str, Any]) -> dict[str, Any]:
        if node.get("type") != "worker":
            raise ValueError(f"unsupported workflow node type: {node.get('type')}")
        prompt = render_prompt(node.get("prompt") or "", input=input, artifacts=artifacts, run=run, node=node, job={})
        payload = {"prompt": prompt}
        for key in ("worker_id", "workspace_id", "workspace_key", "company", "model", "tags"):
            if node.get(key):
                payload[key] = node[key]
        job = self.job_service.submit(payload)
        return self._wait_for_job(job["id"])

    def _wait_for_job(self, job_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.max_wait_seconds
        while True:
            job = self.db.get_job(job_id)
            if job and job["state"] in _JOB_TERMINAL_STATES:
                return job
            if time.monotonic() >= deadline:
                raise TimeoutError(f"workflow job timed out: {job_id}")
            time.sleep(self.poll_interval_seconds)

    def _store_output_artifact(self, run_id: str, node: dict[str, Any], job: dict[str, Any], artifacts: dict[str, Any]) -> list[str]:
        outputs = node.get("outputs") or []
        if not isinstance(outputs, list) or not outputs:
            return []
        key = str(outputs[0])
        content = job.get("assistant_text") or ""
        artifact = self.db.create_artifact(
            {
                "run_id": run_id,
                "job_id": job["id"],
                "key": key,
                "kind": "text",
                "content": content,
                "metadata": {"node": node["id"]},
            }
        )
        artifacts[key] = content
        return [artifact["id"]]


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


def _outgoing_edges(edges: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    outgoing: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        outgoing.setdefault(edge["from"], []).append(edge)
    return outgoing


def _check_limit(policy: dict[str, Any], key: str, count: int) -> None:
    value = policy.get(key)
    if isinstance(value, int) and value > 0 and count >= value:
        raise ValueError(f"workflow policy {key} exceeded")


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
