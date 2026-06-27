from __future__ import annotations

import json
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from .db import Database, now_iso
from .router import Router


_FIELD_RE = re.compile(r"{([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)}")
_JOB_TERMINAL_STATES = {"succeeded", "failed", "cancelled"}
_MANAGER_SCHEMA = "manager_decision_v1"
_TRIGGER_STATES = {"manual", "schedule", "webhook", "workflow_run_completed", "artifact_created", "worker_status_changed"}
_EVENT_TRIGGER_FILTERS = {
    "workflow_run_completed": {"source_workflow_definition_id": "workflow_definition_id", "state": "state"},
    "artifact_created": {"source_workflow_definition_id": "workflow_definition_id", "key": "key", "kind": "kind"},
    "worker_status_changed": {"worker_id": "worker_id", "status": "status"},
}


def validate_workflow_graph(graph: dict[str, Any], policy: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(graph, dict):
        raise ValueError("workflow graph must be an object")
    if policy is not None and not isinstance(policy, dict):
        raise ValueError("workflow policy must be an object")

    nodes = graph.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("workflow graph nodes must be a non-empty list")

    node_ids = set()
    node_map = {}
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise ValueError(f"workflow node at index {index} must be an object")
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id.strip():
            raise ValueError(f"workflow node at index {index} requires a non-empty id")
        if node_id in node_ids:
            raise ValueError(f"duplicate node id: {node_id}")
        node_ids.add(node_id)
        node_map[node_id] = node
        if not isinstance(node.get("type"), str) or not node["type"].strip():
            raise ValueError(f"workflow node {node_id} requires a non-empty type")
        if node["type"] not in {"worker", "manager", "join", "human_gate"}:
            raise ValueError(f"workflow node {node_id} uses unsupported type: {node['type']}")
        if node["type"] == "manager" and node.get("schema", _MANAGER_SCHEMA) != _MANAGER_SCHEMA:
            raise ValueError(f"workflow manager node {node_id} schema must be {_MANAGER_SCHEMA}")
        if node["type"] == "join":
            mode = node.get("mode", "all")
            if not isinstance(mode, str) or mode not in {"all", "any"}:
                raise ValueError(f"workflow join node {node_id} mode must be all or any")

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
        condition = edge.get("condition", {"type": "always"})
        if node_map[edge["from"]]["type"] == "manager" and condition.get("type") != "manager_selected":
            raise ValueError(f"workflow manager edge at index {index} requires manager_selected condition")
        if condition.get("type") == "manager_selected":
            if node_map[edge["from"]]["type"] != "manager":
                raise ValueError(f"workflow edge at index {index} manager_selected requires manager source")
            if condition.get("target") != edge["to"]:
                raise ValueError(f"workflow edge at index {index} manager_selected target must match edge target")

    if _has_cycle(node_ids, edges) and not _has_loop_guard(policy or {}, edges):
        raise ValueError("workflow graph has a cycle; policy.max_iterations or max_iterations_below is required")

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
        self.trigger_service: WorkflowTriggerService | None = None
        self._threads: dict[str, threading.Thread] = {}
        self._thread_lock = threading.RLock()

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

    def start_workflow(self, workflow_definition_id: str, input: dict[str, Any] | None = None) -> dict[str, Any]:
        definition = self.db.get_workflow_definition(workflow_definition_id)
        if not definition:
            raise ValueError(f"Unknown workflow_definition_id: {workflow_definition_id}")
        graph = definition["graph"]
        policy = definition.get("policy") or {}
        input = input or {}
        validate_workflow_graph(graph, policy)
        run = self._create_run(graph, input, workflow_definition_id, definition.get("name") or "Workflow run")
        self._start_background(run["id"], graph, policy, input)
        return self.db.get_workflow_run(run["id"]) or run

    def pause_run(self, run_id: str) -> dict[str, Any]:
        with self._thread_lock:
            run = self.db.get_workflow_run(run_id)
            if not run:
                raise ValueError(f"Unknown workflow_run_id: {run_id}")
            if run["state"] == "paused":
                return run
            if run["state"] != "running":
                raise ValueError(f"workflow run {run_id} cannot be paused from {run['state']}")
            self.db.update_workflow_run(run_id, state="paused")
            self.db.append_workflow_event(run_id, "run_paused")
        return self.db.get_workflow_run(run_id) or run

    def resume_run(self, run_id: str) -> dict[str, Any]:
        with self._thread_lock:
            run = self.db.get_workflow_run(run_id)
            if not run:
                raise ValueError(f"Unknown workflow_run_id: {run_id}")
            if run["state"] != "paused":
                raise ValueError(f"workflow run {run_id} cannot be resumed from {run['state']}")
            definition = self.db.get_workflow_definition(run.get("workflow_definition_id") or "")
            if not definition:
                raise ValueError("workflow definition is unavailable; run cannot be resumed")
            self.db.update_workflow_run(run_id, state="running", error=None, finished_at=None)
            self.db.append_workflow_event(run_id, "run_resumed")
            active = self._threads.get(run_id)
            if not active or not active.is_alive():
                self._spawn_thread(run_id, definition["graph"], definition.get("policy") or {}, run.get("input") or {})
        return self.db.get_workflow_run(run_id) or run

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        with self._thread_lock:
            run = self.db.get_workflow_run(run_id)
            if not run:
                raise ValueError(f"Unknown workflow_run_id: {run_id}")
            if run["state"] in {"succeeded", "failed", "cancelled"}:
                return run
            self.db.update_workflow_run(run_id, state="cancelled", current_nodes=[], finished_at=now_iso())
            self.db.cancel_pending_approvals(run_id)
            self.db.append_workflow_event(run_id, "run_cancelled")
            self.db.append_workflow_event(run_id, "run_finished", {"state": "cancelled"})
        for node in self.db.list_workflow_nodes(run_id):
            if node["state"] == "running" and node.get("job_id"):
                self._cancel_job(node["job_id"])
        self._notify_run_completed(run_id)
        return self.db.get_workflow_run(run_id) or run

    def approve_approval(self, approval_id: str) -> dict[str, Any]:
        with self._thread_lock:
            approval, run = self._pending_approval_context(approval_id)
            definition = self.db.get_workflow_definition(run.get("workflow_definition_id") or "")
            if not definition:
                raise ValueError("workflow definition is unavailable; approval cannot resume the run")
            runtime_node = None
            if approval.get("workflow_node_id"):
                runtime_node = self.db.get_workflow_node(approval["workflow_node_id"])
                if not runtime_node or runtime_node["state"] != "waiting_for_human":
                    raise ValueError("approval workflow node is unavailable")
            approval = self.db.decide_approval(approval_id, "approved")
            counters = run.get("counters") or {}
            self.db.append_workflow_event(
                run["id"],
                "approval_approved",
                {"approval_id": approval_id},
                node_key=approval["node_key"],
            )
            if runtime_node:
                self.db.update_workflow_node(runtime_node["id"], state="succeeded", finished_at=now_iso())
                completed_nodes = counters.setdefault("completed_nodes", [])
                if approval["node_key"] not in completed_nodes:
                    completed_nodes.append(approval["node_key"])
                self.db.append_workflow_event(
                    run["id"],
                    "node_succeeded",
                    {"approval_id": approval_id},
                    node_key=approval["node_key"],
                )
            else:
                counters["requires_human_after_iterations_approved"] = True
            self.db.update_workflow_run(run["id"], state="running", counters=counters, error=None, finished_at=None)
            active = self._threads.get(run["id"])
            if not active or not active.is_alive():
                self._spawn_thread(run["id"], definition["graph"], definition.get("policy") or {}, run.get("input") or {})
        return {"approval": approval, "run": self.db.get_workflow_run(run["id"]) or run}

    def reject_approval(self, approval_id: str) -> dict[str, Any]:
        with self._thread_lock:
            approval, run = self._pending_approval_context(approval_id)
            approval = self.db.decide_approval(approval_id, "rejected")
            error = f"human approval rejected at {approval['node_key']}"
            self.db.append_workflow_event(
                run["id"],
                "approval_rejected",
                {"approval_id": approval_id, "error": error},
                node_key=approval["node_key"],
            )
            if approval.get("workflow_node_id"):
                runtime_node = self.db.get_workflow_node(approval["workflow_node_id"])
                if runtime_node:
                    self.db.update_workflow_node(runtime_node["id"], state="failed", error=error, finished_at=now_iso())
                    self.db.append_workflow_event(run["id"], "node_failed", {"error": error}, node_key=approval["node_key"])
            self._finish_run(run["id"], "failed", run.get("counters") or {}, error)
        return {"approval": approval, "run": self.db.get_workflow_run(run["id"]) or run}

    def _pending_approval_context(self, approval_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        approval = self.db.get_approval(approval_id)
        if not approval:
            raise ValueError(f"Unknown approval_id: {approval_id}")
        if approval["state"] != "pending":
            raise ValueError(f"approval {approval_id} already {approval['state']}")
        run = self.db.get_workflow_run(approval["run_id"])
        if not run or run["state"] != "waiting_for_human":
            raise ValueError("approval run is not waiting_for_human")
        return approval, run

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
        run = self._create_run(graph, input, workflow_definition_id, name)
        return self._execute_run(run["id"], graph, policy, input)

    def _create_run(
        self,
        graph: dict[str, Any],
        input: dict[str, Any],
        workflow_definition_id: str | None,
        name: str,
    ) -> dict[str, Any]:
        return self.db.create_workflow_run(
            {
                "workflow_definition_id": workflow_definition_id,
                "name": name,
                "state": "running",
                "input": input,
                "current_nodes": [graph["start"]],
                "counters": {
                    "jobs_started": 0,
                    "node_counts": {},
                    "completed_nodes": [],
                    "join_states": _initial_join_states(graph),
                },
                "started_at": now_iso(),
            }
        )

    def _start_background(
        self,
        run_id: str,
        graph: dict[str, Any],
        policy: dict[str, Any],
        input: dict[str, Any],
    ) -> None:
        with self._thread_lock:
            self._spawn_thread(run_id, graph, policy, input)

    def _spawn_thread(
        self,
        run_id: str,
        graph: dict[str, Any],
        policy: dict[str, Any],
        input: dict[str, Any],
    ) -> None:
        thread = threading.Thread(
            target=self._run_background,
            args=(run_id, graph, policy, input),
            name=f"atlas-workflow-{run_id}",
            daemon=True,
        )
        self._threads[run_id] = thread
        thread.start()

    def _run_background(self, run_id: str, graph: dict[str, Any], policy: dict[str, Any], input: dict[str, Any]) -> None:
        try:
            self._execute_run(run_id, graph, policy, input)
        finally:
            with self._thread_lock:
                if self._threads.get(run_id) is threading.current_thread():
                    self._threads.pop(run_id, None)

    def _execute_run(
        self,
        run_id: str,
        graph: dict[str, Any],
        policy: dict[str, Any],
        input: dict[str, Any],
    ) -> dict[str, Any]:
        validate_workflow_graph(graph, policy)
        node_map = {node["id"]: node for node in graph["nodes"]}
        edges = graph.get("edges", [])
        outgoing = _outgoing_edges(edges)
        cycle_edges = _cycle_edges(edges)
        run = self.db.get_workflow_run(run_id)
        if not run:
            raise ValueError(f"Unknown workflow_run_id: {run_id}")
        ready = list(run.get("current_nodes") or [])
        artifacts = self._load_artifacts(run_id)
        counters = run.get("counters") or {"jobs_started": 0, "node_counts": {}}
        counters.setdefault("jobs_started", 0)
        counters.setdefault("node_counts", {})
        restore_completed_nodes = "completed_nodes" not in counters
        completed_nodes = counters.setdefault("completed_nodes", [])
        if restore_completed_nodes:
            for runtime_node in self.db.list_workflow_nodes(run_id):
                if runtime_node["state"] == "succeeded" and runtime_node["node_key"] not in completed_nodes:
                    completed_nodes.append(runtime_node["node_key"])
        completed = set(completed_nodes)
        join_states = counters.setdefault("join_states", {})
        for node_key, state in _initial_join_states(graph).items():
            join_states.setdefault(node_key, state)
        deadline = _workflow_deadline(run, policy)

        def schedule_outgoing(node_key: str, node: dict[str, Any], manager_decision: dict[str, Any] | None = None) -> None:
            manager_selected = set((manager_decision or {}).get("selected_nodes") or [])
            for edge in outgoing.get(node_key, []):
                condition = edge.get("condition", {"type": "always"})
                condition_result = _evaluate_condition(condition, artifacts, counters, manager_selected)
                if condition_result["matched"]:
                    self.db.append_workflow_edge(run_id, edge["from"], edge["to"], condition_result)
                    self.db.append_workflow_event(
                        run_id,
                        "edge_taken",
                        {"to": edge["to"], "condition_result": condition_result},
                        node_key=node_key,
                    )
                    _schedule_node(
                        edge,
                        ready,
                        completed,
                        completed_nodes,
                        node_map,
                        join_states,
                        cycle_edges,
                        prioritize=node.get("type") == "join",
                    )
                else:
                    if condition.get("type") == "max_iterations_below":
                        self.db.append_workflow_event(run_id, "guard_tripped", condition_result, node_key=node_key)
                    self.db.append_workflow_event(
                        run_id,
                        "condition_skipped",
                        {"to": edge["to"], "condition_result": condition_result},
                        node_key=node_key,
                    )

        try:
            while ready:
                runtime_node = None
                try:
                    with self._thread_lock:
                        if self._stop_requested(run_id, ready, counters):
                            return self.db.get_workflow_run(run_id) or run
                        _check_deadline(deadline)
                        node_key = ready.pop(0)
                        if node_key in completed:
                            self.db.update_workflow_run(run_id, current_nodes=ready, counters=counters)
                            continue
                        node = node_map[node_key]
                        node_type = node.get("type")
                        if node_type in {"worker", "manager"}:
                            if _requires_human_after_iterations(policy, counters):
                                ready.insert(0, node_key)
                                approval = self.db.create_approval(
                                    {
                                        "run_id": run_id,
                                        "node_key": node_key,
                                        "approval_key": "policy:requires_human_after_iterations",
                                        "label": "Continue workflow",
                                        "reason": f"Workflow reached {counters['jobs_started']} iterations",
                                    }
                                )
                                self._wait_for_human(run_id, ready, counters, approval)
                                return self.db.get_workflow_run(run_id) or run
                            _check_limit(policy, "max_jobs", counters["jobs_started"])
                            _check_limit(policy, "max_iterations", counters["jobs_started"])
                        node_counts = counters["node_counts"]
                        node_counts[node_key] = int(node_counts.get(node_key) or 0) + 1
                        _check_limit(policy, "max_attempts_per_node", node_counts[node_key] - 1)
                        self.db.update_workflow_run(run_id, current_nodes=[node_key, *ready], counters=counters)
                        runtime_node = self.db.create_workflow_node(
                            {
                                "run_id": run_id,
                                "node_key": node_key,
                                "state": "running",
                                "attempt": node_counts[node_key],
                                "input_artifacts": list(artifacts),
                                "started_at": now_iso(),
                            }
                        )
                        self.db.append_workflow_event(
                            run_id,
                            "node_started",
                            {"attempt": node_counts[node_key]},
                            node_key=node_key,
                        )
                        job = None
                        if node_type == "human_gate":
                            approval = self.db.create_approval(
                                {
                                    "run_id": run_id,
                                    "workflow_node_id": runtime_node["id"],
                                    "node_key": node_key,
                                    "approval_key": f"human_gate:{node_key}:{node_counts[node_key]}",
                                    "label": node.get("label") or f"Approve {node_key}",
                                    "reason": node.get("reason") or "Workflow reached a human gate",
                                }
                            )
                            self.db.update_workflow_node(runtime_node["id"], state="waiting_for_human")
                            schedule_outgoing(node_key, node)
                            self._wait_for_human(run_id, ready, counters, approval)
                            return self.db.get_workflow_run(run_id) or run
                        if node_type in {"worker", "manager"}:
                            job = self._submit_worker_node(run, node, input, artifacts, policy, graph, counters)
                            self.db.update_workflow_node(runtime_node["id"], job_id=job["id"])
                    output_artifacts: list[str] = []
                    manager_decision = None
                    if job:
                        job = self._wait_for_job(job["id"], run["id"], deadline)
                        counters["jobs_started"] += 1
                        if job["state"] != "succeeded":
                            raise ValueError(f"workflow node {node_key} job {job['id']} ended as {job['state']}")
                        if node_type == "manager":
                            manager_decision = self._validate_manager_decision(
                                run,
                                graph,
                                node,
                                job,
                                input,
                                artifacts,
                                counters,
                                policy,
                                outgoing.get(node_key, []),
                                deadline,
                            )
                        else:
                            output_artifacts = self._store_output_artifact(run_id, node, job, artifacts)
                    self.db.update_workflow_node(
                        runtime_node["id"],
                        state="succeeded",
                        job_id=job["id"] if job else None,
                        output_artifacts=output_artifacts,
                        finished_at=now_iso(),
                    )
                    if node_key not in completed:
                        completed.add(node_key)
                        completed_nodes.append(node_key)
                    if node.get("type") == "join":
                        join_states[node_key]["state"] = "succeeded"
                    event_payload = {"output_artifacts": output_artifacts}
                    if job:
                        event_payload["job_id"] = job["id"]
                    if manager_decision:
                        event_payload["manager_decision"] = manager_decision
                    else:
                        if node.get("type") == "join":
                            event_payload["join"] = join_states[node_key]
                    self.db.append_workflow_event(
                        run_id,
                        "node_succeeded",
                        event_payload,
                        node_key=node_key,
                    )
                except _WorkflowCancelled:
                    if runtime_node:
                        self.db.update_workflow_node(runtime_node["id"], state="cancelled", finished_at=now_iso())
                    raise
                except Exception as exc:
                    if runtime_node:
                        self.db.update_workflow_node(runtime_node["id"], state="failed", error=str(exc), finished_at=now_iso())
                        self.db.append_workflow_event(run_id, "node_failed", {"error": str(exc)}, node_key=node_key)
                    raise

                schedule_outgoing(node_key, node, manager_decision)
                self.db.update_workflow_run(run_id, current_nodes=ready, counters=counters)

            if (self.db.get_workflow_run(run_id) or {}).get("state") == "cancelled":
                return self.db.get_workflow_run(run_id) or run
            self._finish_run(run_id, "succeeded", counters)
        except _WorkflowCancelled:
            return self.db.get_workflow_run(run_id) or run
        except _WorkflowGuardTripped as exc:
            self.db.append_workflow_event(run_id, "guard_tripped", {"error": str(exc)})
            self._finish_run(run_id, "failed", counters, str(exc))
        except Exception as exc:
            if (self.db.get_workflow_run(run_id) or {}).get("state") != "cancelled":
                self._finish_run(run_id, "failed", counters, str(exc))
        return self.db.get_workflow_run(run_id) or run

    def _stop_requested(self, run_id: str, ready: list[str], counters: dict[str, Any]) -> bool:
        run = self.db.get_workflow_run(run_id)
        if not run or run["state"] == "cancelled":
            return True
        if run["state"] not in {"paused", "waiting_for_human"}:
            return False
        with self._thread_lock:
            run = self.db.get_workflow_run(run_id)
            if run and run["state"] in {"paused", "waiting_for_human"}:
                self.db.update_workflow_run(run_id, current_nodes=ready, counters=counters)
                if self._threads.get(run_id) is threading.current_thread():
                    self._threads.pop(run_id, None)
                return True
        return False

    def _wait_for_human(
        self,
        run_id: str,
        ready: list[str],
        counters: dict[str, Any],
        approval: dict[str, Any],
    ) -> None:
        if approval.get("state") != "pending":
            raise ValueError(f"approval {approval.get('id')} already {approval.get('state')}")
        self.db.update_workflow_run(run_id, state="waiting_for_human", current_nodes=ready, counters=counters)
        self.db.append_workflow_event(
            run_id,
            "approval_created",
            {"approval_id": approval["id"], "label": approval["label"], "reason": approval["reason"]},
            node_key=approval["node_key"],
        )
        if self._threads.get(run_id) is threading.current_thread():
            self._threads.pop(run_id, None)

    def _finish_run(self, run_id: str, state: str, counters: dict[str, Any], error: str | None = None) -> None:
        self.db.update_workflow_run(
            run_id,
            state=state,
            current_nodes=[],
            counters=counters,
            error=error,
            finished_at=now_iso(),
        )
        self.db.append_workflow_event(run_id, "run_finished", {"state": state, "error": error})
        self._notify_run_completed(run_id)

    def _notify_run_completed(self, run_id: str) -> None:
        if not self.trigger_service:
            return
        run = self.db.get_workflow_run(run_id)
        if run:
            self.trigger_service.fire_internal(
                "workflow_run_completed",
                {
                    "run_id": run_id,
                    "workflow_definition_id": run.get("workflow_definition_id"),
                    "state": run["state"],
                    "error": run.get("error"),
                },
                f"workflow_run_completed:{run_id}:{run['state']}",
            )

    def _validate_manager_decision(
        self,
        run: dict[str, Any],
        graph: dict[str, Any],
        manager: dict[str, Any],
        job: dict[str, Any],
        input: dict[str, Any],
        artifacts: dict[str, Any],
        counters: dict[str, Any],
        policy: dict[str, Any],
        outgoing: list[dict[str, Any]],
        deadline: datetime | None,
    ) -> dict[str, Any]:
        try:
            proposal = _parse_manager_decision(job.get("assistant_text") or "")
        except ValueError as exc:
            decision = {
                "manager": manager["id"],
                "state": "rejected",
                "reason": str(exc),
                "job_id": job["id"],
                "response": job.get("assistant_text") or "",
                "selected_nodes": [],
            }
            self._record_manager_decision(run["id"], decision, counters)
            raise ValueError(f"manager node {manager['id']} returned invalid JSON: {exc}") from exc

        node_map = {node["id"]: node for node in graph["nodes"]}
        allowed_targets = {edge["to"] for edge in outgoing}
        selected_nodes: list[str] = []
        item_results: list[dict[str, Any]] = []
        errors: list[Exception] = []
        for action in proposal["next"]:
            target = action["node"]
            try:
                if target not in node_map:
                    raise ValueError(f"target node does not exist: {target}")
                if target not in allowed_targets:
                    raise ValueError(f"manager has no outgoing edge to target: {target}")
                missing = [key for key in action["input_artifacts"] if key not in artifacts]
                if missing:
                    raise ValueError(f"required artifacts are missing for {target}: {', '.join(missing)}")
                _check_deadline(deadline)
                target_node = node_map[target]
                target_count = int((counters.get("node_counts") or {}).get(target) or 0)
                _check_limit(policy, "max_attempts_per_node", target_count)
                if target_node.get("type") in {"worker", "manager"}:
                    _check_limit(policy, "max_jobs", int(counters.get("jobs_started") or 0))
                    _check_limit(policy, "max_iterations", int(counters.get("jobs_started") or 0))
                    self._prepare_worker_node_payload(
                        run,
                        target_node,
                        input,
                        artifacts,
                        policy,
                        graph,
                        counters,
                        consume_manager_action=False,
                    )
                if target in selected_nodes:
                    item_results.append({"node": target, "accepted": False, "reason": "duplicate target ignored"})
                    continue
                selected_nodes.append(target)
                item_results.append({"node": target, "accepted": True, "reason": "accepted"})
            except ValueError as exc:
                errors.append(exc)
                item_results.append({"node": target, "accepted": False, "reason": str(exc)})

        if errors:
            reason = "; ".join(str(error) for error in errors)
            for item in item_results:
                if item["accepted"]:
                    item.update(accepted=False, reason="not scheduled because another proposal item was rejected")
            decision = {
                "manager": manager["id"],
                "state": "rejected",
                "reason": reason,
                "job_id": job["id"],
                "proposal": proposal,
                "items": item_results,
                "selected_nodes": [],
            }
            self._record_manager_decision(run["id"], decision, counters)
            guard = next((error for error in errors if isinstance(error, _WorkflowGuardTripped)), None)
            if guard:
                raise guard
            raise ValueError(f"manager proposal rejected: {reason}")

        actions = counters.setdefault("manager_actions", {})
        for target in selected_nodes:
            action = next(item for item in proposal["next"] if item["node"] == target)
            if node_map[target].get("type") == "worker":
                actions[target] = action
        decision = {
            "manager": manager["id"],
            "state": "accepted",
            "reason": proposal["reason"] or ("manager requested stop" if proposal["stop"] else "proposal accepted"),
            "job_id": job["id"],
            "proposal": proposal,
            "items": item_results,
            "selected_nodes": selected_nodes,
        }
        self._record_manager_decision(run["id"], decision, counters)
        return decision

    def _record_manager_decision(self, run_id: str, decision: dict[str, Any], counters: dict[str, Any]) -> None:
        decisions = counters.setdefault("manager_decisions", [])
        decisions.append(decision)
        self.db.update_workflow_run(run_id, counters=counters)
        event_type = f"manager_proposal_{decision['state']}"
        self.db.append_workflow_event(run_id, event_type, decision, node_key=decision["manager"])
        self.db.audit(f"workflow.{event_type}", "workflow_run", run_id, decision)

    def _submit_worker_node(
        self,
        run: dict[str, Any],
        node: dict[str, Any],
        input: dict[str, Any],
        artifacts: dict[str, Any],
        policy: dict[str, Any],
        graph: dict[str, Any],
        counters: dict[str, Any],
    ) -> dict[str, Any]:
        return self.job_service.submit(self._prepare_worker_node_payload(run, node, input, artifacts, policy, graph, counters))

    def _prepare_worker_node_payload(
        self,
        run: dict[str, Any],
        node: dict[str, Any],
        input: dict[str, Any],
        artifacts: dict[str, Any],
        policy: dict[str, Any],
        graph: dict[str, Any],
        counters: dict[str, Any],
        consume_manager_action: bool = True,
    ) -> dict[str, Any]:
        if node.get("type") not in {"worker", "manager"}:
            raise ValueError(f"unsupported workflow node type: {node.get('type')}")
        if node.get("type") == "manager":
            prompt = _manager_prompt(graph, node, artifacts, counters, policy)
        else:
            prompt = render_prompt(node.get("prompt") or "", input=input, artifacts=artifacts, run=run, node=node, job={})
            if consume_manager_action:
                action = (counters.get("manager_actions") or {}).pop(node["id"], None)
                if action and action["instructions"].strip():
                    prompt = f"{action['instructions'].strip()}\n\n{prompt}".strip()
        payload = {"prompt": prompt}
        for key in ("worker_id", "workspace_id", "workspace_key", "company", "model", "tags", "role"):
            if node.get(key):
                payload[key] = node[key]
        for key in ("allowed_worker_ids", "allowed_workspace_ids"):
            value = policy.get(key)
            if value is not None and (not isinstance(value, list) or not all(isinstance(item, str) and item for item in value)):
                raise ValueError(f"workflow policy {key} must be a list of ids")
            if value:
                payload[key] = value
        decision = Router(self.db).resolve(payload)
        payload["worker_id"] = decision.worker["id"]
        if decision.workspace:
            payload["workspace_id"] = decision.workspace["id"]
        return payload

    def _wait_for_job(self, job_id: str, run_id: str | None = None, deadline_at: datetime | None = None) -> dict[str, Any]:
        wait_deadline = time.monotonic() + self.max_wait_seconds
        while True:
            if run_id:
                run = self.db.get_workflow_run(run_id)
                if not run or run["state"] == "cancelled":
                    self._cancel_job(job_id)
                    raise _WorkflowCancelled()
            if deadline_at is not None and datetime.now(UTC) >= deadline_at:
                self._cancel_job(job_id)
                raise _WorkflowGuardTripped("workflow policy max_minutes exceeded")
            job = self.db.get_job(job_id)
            if job and job["state"] in _JOB_TERMINAL_STATES:
                return job
            if time.monotonic() >= wait_deadline:
                raise TimeoutError(f"workflow job timed out: {job_id}")
            time.sleep(self.poll_interval_seconds)

    def _cancel_job(self, job_id: str) -> None:
        cancel = getattr(self.job_service, "cancel", None)
        if callable(cancel):
            try:
                cancel(job_id)
            except Exception:
                pass

    def _load_artifacts(self, run_id: str) -> dict[str, Any]:
        artifacts: dict[str, Any] = {}
        for artifact in reversed(self.db.list_artifacts(run_id=run_id, limit=1000)):
            value: Any = artifact.get("content") or ""
            if artifact.get("kind") == "json":
                value = json.loads(value)
            artifacts[artifact["key"]] = value
        return artifacts

    def _store_output_artifact(self, run_id: str, node: dict[str, Any], job: dict[str, Any], artifacts: dict[str, Any]) -> list[str]:
        outputs = node.get("outputs") or []
        if not isinstance(outputs, list) or not outputs:
            return []
        key = str(outputs[0])
        content = job.get("assistant_text") or ""
        value: Any = content
        kind = "text"
        if node.get("output_format") == "json":
            value = json.loads(content)
            kind = "json"
            content = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
        artifact = self.db.create_artifact(
            {
                "run_id": run_id,
                "job_id": job["id"],
                "key": key,
                "kind": kind,
                "content": content,
                "metadata": {"node": node["id"]},
            }
        )
        artifacts[key] = value
        if self.trigger_service:
            self.trigger_service.artifact_created(artifact)
        return [artifact["id"]]


class WorkflowTriggerService:
    def __init__(self, db: Database, runner: WorkflowRunner, poll_seconds: float = 30):
        self.db = db
        self.runner = runner
        self.runner.trigger_service = self
        self.runner.job_service.trigger_service = self
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="atlas-workflow-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def scheduler_tick(self) -> None:
        now = datetime.now(UTC).replace(microsecond=0)
        for trigger in self.db.list_workflow_triggers(limit=500, enabled=True):
            if trigger.get("type") != "schedule":
                continue
            next_fire_at = trigger.get("next_fire_at") or next_fire_at_for_trigger(trigger, now)
            if _parse_utc(next_fire_at) > now:
                continue
            self.fire_trigger(
                trigger["id"],
                payload={"trigger_id": trigger["id"], "scheduled_at": next_fire_at},
                dedupe_key=f"{trigger['id']}:{next_fire_at}",
            )

    def fire_trigger(self, trigger_id: str, payload: dict[str, Any] | None = None, dedupe_key: str | None = None) -> dict[str, Any]:
        trigger = self.db.get_workflow_trigger(trigger_id)
        if not trigger:
            raise ValueError(f"Unknown workflow_trigger_id: {trigger_id}")
        payload = payload or {}

        if dedupe_key and self.db.has_workflow_trigger_event_dedupe(trigger_id, dedupe_key):
            event = self.db.append_workflow_trigger_event(trigger_id, "ignored", payload=payload, dedupe_key=dedupe_key, error="duplicate dedupe_key")
            return {"trigger": trigger, "event": event, "run": None}

        self.db.append_workflow_trigger_event(trigger_id, "received", payload=payload, dedupe_key=dedupe_key)
        fired_at = now_iso()
        try:
            run = self.runner.start_workflow(trigger["workflow_definition_id"], payload)
            event = self.db.append_workflow_trigger_event(trigger_id, "started", payload=payload, run_id=run["id"], dedupe_key=dedupe_key)
            updated = self.db.update_workflow_trigger(
                trigger_id,
                {
                    "last_fired_at": fired_at,
                    "next_fire_at": next_fire_at_for_trigger(trigger, datetime.now(UTC)) if trigger.get("type") == "schedule" else None,
                },
            )
            return {"trigger": updated or trigger, "event": event, "run": run}
        except Exception as exc:
            event = self.db.append_workflow_trigger_event(trigger_id, "failed", payload=payload, error=str(exc), dedupe_key=dedupe_key)
            updated = self.db.update_workflow_trigger(
                trigger_id,
                {
                    "last_fired_at": fired_at,
                    "next_fire_at": next_fire_at_for_trigger(trigger, datetime.now(UTC)) if trigger.get("type") == "schedule" else None,
                },
            )
            return {"trigger": updated or trigger, "event": event, "run": None}

    def artifact_created(self, artifact: dict[str, Any]) -> list[dict[str, Any]]:
        run = self.db.get_workflow_run(artifact.get("run_id") or "") or {}
        return self.fire_internal(
            "artifact_created",
            {
                "artifact_id": artifact["id"],
                "run_id": artifact.get("run_id"),
                "workflow_definition_id": run.get("workflow_definition_id"),
                "job_id": artifact.get("job_id"),
                "key": artifact["key"],
                "kind": artifact["kind"],
            },
            f"artifact_created:{artifact['id']}",
        )

    def fire_internal(self, event_type: str, payload: dict[str, Any], dedupe_key: str) -> list[dict[str, Any]]:
        results = []
        event_payload = {**payload, "event_type": event_type}
        try:
            triggers = self.db.list_workflow_triggers(limit=500, enabled=True)
            for trigger in triggers:
                if trigger.get("type") != event_type or not _event_trigger_matches(trigger, event_payload):
                    continue
                # ponytail: self-triggering needs an explicit guarded design, otherwise one completion can loop forever.
                if event_type in {"workflow_run_completed", "artifact_created"} and trigger["workflow_definition_id"] == payload.get("workflow_definition_id"):
                    continue
                results.append(self.fire_trigger(trigger["id"], event_payload, dedupe_key))
        except Exception as exc:
            self.db.audit("workflow_trigger.internal_error", "workflow_trigger", event_type, {"error": str(exc)})
        return results

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.scheduler_tick()
            except Exception:
                pass


def validate_workflow_trigger_payload(payload: dict[str, Any]) -> None:
    trigger_type = payload.get("type") or "manual"
    if not isinstance(trigger_type, str) or trigger_type not in _TRIGGER_STATES:
        raise ValueError(f"unsupported workflow trigger type: {trigger_type}")
    if payload.get("config") is not None and not isinstance(payload.get("config"), dict):
        raise ValueError("workflow trigger config must be an object")
    if trigger_type == "schedule":
        next_fire_at_for_trigger(payload)


def next_fire_at_for_trigger(trigger: dict[str, Any], base: datetime | None = None) -> str | None:
    trigger_type = trigger.get("type") or "manual"
    if not isinstance(trigger_type, str) or trigger_type not in _TRIGGER_STATES:
        raise ValueError(f"unsupported workflow trigger type: {trigger_type}")
    if trigger_type != "schedule":
        return None

    config = trigger.get("config") or {}
    base = (base or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    if "interval_minutes" in config:
        try:
            minutes = float(config["interval_minutes"])
        except (TypeError, ValueError) as exc:
            raise ValueError("schedule interval_minutes must be positive") from exc
        if minutes <= 0:
            raise ValueError("schedule interval_minutes must be positive")
        return _iso_utc(base + timedelta(minutes=minutes))

    daily_time = config.get("daily_time")
    if isinstance(daily_time, str) and re.fullmatch(r"\d{2}:\d{2}", daily_time):
        hour, minute = [int(part) for part in daily_time.split(":")]
        if hour > 23 or minute > 59:
            raise ValueError("schedule daily_time must be HH:MM")
        local_now = base.astimezone()
        candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local_now:
            candidate += timedelta(days=1)
        return _iso_utc(candidate.astimezone(UTC))

    raise ValueError("schedule config requires interval_minutes or daily_time")


def _event_trigger_matches(trigger: dict[str, Any], payload: dict[str, Any]) -> bool:
    config = trigger.get("config") or {}
    return all(config.get(key) == payload.get(payload_key) for key, payload_key in _EVENT_TRIGGER_FILTERS[trigger["type"]].items() if key in config)


def _validate_edge(edge: Any, index: int, node_ids: set[str]) -> None:
    if not isinstance(edge, dict):
        raise ValueError(f"workflow edge at index {index} must be an object")
    from_node = edge.get("from")
    to_node = edge.get("to")
    if not isinstance(from_node, str) or from_node not in node_ids:
        raise ValueError(f"workflow edge at index {index} references missing from node: {from_node}")
    if not isinstance(to_node, str) or to_node not in node_ids:
        raise ValueError(f"workflow edge at index {index} references missing to node: {to_node}")
    _validate_condition(edge.get("condition", {"type": "always"}), index, node_ids)


def _validate_condition(condition: Any, edge_index: int, node_ids: set[str]) -> None:
    if not isinstance(condition, dict):
        raise ValueError(f"workflow edge at index {edge_index} condition must be an object")
    condition_type = condition.get("type")
    if condition_type == "always":
        return
    if condition_type == "artifact_equals":
        if not condition.get("artifact"):
            raise ValueError(f"workflow edge at index {edge_index} artifact_equals requires artifact")
        if "value" not in condition:
            raise ValueError(f"workflow edge at index {edge_index} artifact_equals requires value")
        return
    if condition_type == "artifact_in":
        if not condition.get("artifact"):
            raise ValueError(f"workflow edge at index {edge_index} artifact_in requires artifact")
        if not isinstance(condition.get("values"), list):
            raise ValueError(f"workflow edge at index {edge_index} artifact_in requires values list")
        return
    if condition_type == "manager_selected":
        if condition.get("target") not in node_ids:
            raise ValueError(f"workflow edge at index {edge_index} manager_selected references missing target")
        return
    if condition_type == "max_iterations_below":
        if condition.get("node") not in node_ids:
            raise ValueError(f"workflow edge at index {edge_index} max_iterations_below references missing node")
        if not isinstance(condition.get("max"), int) or condition["max"] <= 0:
            raise ValueError(f"workflow edge at index {edge_index} max_iterations_below requires positive max")
        return
    raise ValueError(f"workflow edge at index {edge_index} uses unsupported condition: {condition_type}")


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


def _initial_join_states(graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    incoming: dict[str, list[str]] = {}
    for edge in graph.get("edges", []):
        sources = incoming.setdefault(edge["to"], [])
        if edge["from"] not in sources:
            sources.append(edge["from"])
    return {
        node["id"]: {
            "mode": node.get("mode") or "all",
            "state": "ready" if node["id"] == graph.get("start") else "waiting",
            "upstream_nodes": incoming.get(node["id"], []),
            "completed_upstreams": [],
        }
        for node in graph.get("nodes", [])
        if node.get("type") == "join"
    }


def _cycle_edges(edges: list[dict[str, Any]]) -> set[tuple[str, str]]:
    # ponytail: workflow graphs are small; use SCC indexing only if this scan becomes measurable.
    outgoing = _outgoing_edges(edges)

    def reaches(start: str, target: str) -> bool:
        pending = [start]
        visited = set()
        while pending:
            node = pending.pop()
            if node == target:
                return True
            if node not in visited:
                visited.add(node)
                pending.extend(edge["to"] for edge in outgoing.get(node, []))
        return False

    return {(edge["from"], edge["to"]) for edge in edges if reaches(edge["to"], edge["from"])}


def _schedule_node(
    edge: dict[str, Any],
    ready: list[str],
    completed: set[str],
    completed_nodes: list[str],
    node_map: dict[str, dict[str, Any]],
    join_states: dict[str, dict[str, Any]],
    cycle_edges: set[tuple[str, str]],
    prioritize: bool = False,
) -> None:
    source = edge["from"]
    target = edge["to"]
    is_cycle = (source, target) in cycle_edges
    if is_cycle and target in completed:
        completed.remove(target)
        completed_nodes.remove(target)
        if target in join_states:
            join_states[target]["state"] = "waiting"
            join_states[target]["completed_upstreams"] = []

    if node_map[target].get("type") == "join":
        state = join_states[target]
        if source not in state["completed_upstreams"]:
            state["completed_upstreams"].append(source)
        satisfied = (
            bool(state["completed_upstreams"])
            if state["mode"] == "any"
            else set(state["upstream_nodes"]) <= set(state["completed_upstreams"])
        )
        if target in completed or not satisfied:
            return
        state["state"] = "ready"
        prioritize = True

    if target in completed or target in ready:
        return
    if prioritize:
        ready.insert(0, target)
    else:
        ready.append(target)


def _check_limit(policy: dict[str, Any], key: str, count: int) -> None:
    value = policy.get(key)
    if isinstance(value, int) and value > 0 and count >= value:
        raise _WorkflowGuardTripped(f"workflow policy {key} exceeded")


def _requires_human_after_iterations(policy: dict[str, Any], counters: dict[str, Any]) -> bool:
    value = policy.get("requires_human_after_iterations")
    return (
        isinstance(value, int)
        and value > 0
        and int(counters.get("jobs_started") or 0) >= value
        and not counters.get("requires_human_after_iterations_approved")
    )


def _workflow_deadline(run: dict[str, Any], policy: dict[str, Any]) -> datetime | None:
    max_minutes = policy.get("max_minutes")
    if not isinstance(max_minutes, (int, float)) or max_minutes <= 0:
        return None
    started_at = run.get("started_at") or run.get("created_at")
    return _parse_utc(started_at) + timedelta(minutes=max_minutes)


def _check_deadline(deadline: datetime | None) -> None:
    if deadline is not None and datetime.now(UTC) >= deadline:
        raise _WorkflowGuardTripped("workflow policy max_minutes exceeded")


def _has_loop_guard(policy: dict[str, Any], edges: list[dict[str, Any]] | None = None) -> bool:
    value = policy.get("max_iterations")
    if isinstance(value, int) and value > 0:
        return True
    return any((edge.get("condition") or {}).get("type") == "max_iterations_below" for edge in edges or [])


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


def _manager_prompt(
    graph: dict[str, Any],
    node: dict[str, Any],
    artifacts: dict[str, Any],
    counters: dict[str, Any],
    policy: dict[str, Any],
) -> str:
    context = {
        "graph": graph,
        "current_node": node,
        "artifacts": artifacts,
        "counters": counters,
        "policy": policy,
    }
    return (
        f"{str(node.get('prompt') or 'Choose the next workflow action.').strip()}\n\n"
        "Return JSON only using manager_decision_v1: "
        '{"stop":false,"reason":"...","next":[{"node":"target","input_artifacts":[],"instructions":"..."}]}.\n'
        f"Manager context JSON:\n{json.dumps(context, ensure_ascii=True, separators=(',', ':'))}"
    )


def _parse_manager_decision(text: str) -> dict[str, Any]:
    try:
        decision = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise ValueError(f"response must be one JSON object: {exc.msg}") from exc
    if not isinstance(decision, dict):
        raise ValueError("response must be a JSON object")
    if not isinstance(decision.get("stop"), bool):
        raise ValueError("manager decision stop must be boolean")
    if not isinstance(decision.get("reason"), str):
        raise ValueError("manager decision reason must be a string")
    actions = decision.get("next")
    if not isinstance(actions, list):
        raise ValueError("manager decision next must be a list")
    if decision["stop"] and actions:
        raise ValueError("manager decision next must be empty when stop is true")
    if not decision["stop"] and not actions:
        raise ValueError("manager decision next must not be empty when stop is false")
    normalized = []
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            raise ValueError(f"manager decision next item {index} must be an object")
        node = action.get("node")
        if not isinstance(node, str) or not node.strip():
            raise ValueError(f"manager decision next item {index} requires node")
        input_artifacts = action.get("input_artifacts")
        if not isinstance(input_artifacts, list) or not all(isinstance(key, str) and key for key in input_artifacts):
            raise ValueError(f"manager decision next item {index} input_artifacts must be a list of keys")
        instructions = action.get("instructions")
        if not isinstance(instructions, str):
            raise ValueError(f"manager decision next item {index} instructions must be a string")
        normalized.append({"node": node, "input_artifacts": input_artifacts, "instructions": instructions})
    return {"stop": decision["stop"], "reason": decision["reason"], "next": normalized}


def _evaluate_condition(
    condition: dict[str, Any],
    artifacts: dict[str, Any],
    counters: dict[str, Any],
    manager_selected: set[str] | None = None,
) -> dict[str, Any]:
    condition_type = condition.get("type") or "always"
    if condition_type == "always":
        return {"type": "always", "matched": True}
    if condition_type == "artifact_equals":
        actual = _artifact_condition_value(condition, artifacts)
        return {"type": condition_type, "matched": actual == condition.get("value"), "actual": actual}
    if condition_type == "artifact_in":
        actual = _artifact_condition_value(condition, artifacts)
        return {"type": condition_type, "matched": actual in condition.get("values", []), "actual": actual}
    if condition_type == "manager_selected":
        target = condition.get("target")
        return {"type": condition_type, "matched": target in (manager_selected or set()), "target": target}
    if condition_type == "max_iterations_below":
        count = int((counters.get("node_counts") or {}).get(condition["node"]) or 0)
        return {"type": condition_type, "matched": count < int(condition["max"]), "actual": count}
    raise ValueError(f"unsupported workflow condition: {condition_type}")


def _artifact_condition_value(condition: dict[str, Any], artifacts: dict[str, Any]) -> Any:
    value = artifacts.get(condition.get("artifact"))
    path = condition.get("path")
    if not path:
        return value
    for part in str(path).split("."):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit():
            value = value[int(part)] if int(part) < len(value) else None
        else:
            return None
    return value


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class _WorkflowCancelled(Exception):
    pass


class _WorkflowGuardTripped(ValueError):
    pass
