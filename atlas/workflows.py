from __future__ import annotations

import contextvars
import json
import logging
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from .db import Database, now_iso
from .router import Router
from .usage import elapsed_seconds


_FIELD_RE = re.compile(r"{([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)}")
_JOB_TERMINAL_STATES = {"succeeded", "failed", "cancelled"}
_MANAGER_SCHEMA = "manager_decision_v1"
_TRIGGER_STATES = {"manual", "schedule", "webhook", "workflow_run_completed", "artifact_created", "worker_status_changed"}
_EVENT_TRIGGER_FILTERS = {
    "workflow_run_completed": {"source_workflow_definition_id": "workflow_definition_id", "state": "state"},
    "artifact_created": {"source_workflow_definition_id": "workflow_definition_id", "key": "key", "kind": "kind"},
    "worker_status_changed": {"worker_id": "worker_id", "status": "status"},
}
# Allowed config keys for the trigger types whose config is a CLOSED object in
# docs/specs/workflow-trigger.schema.json (additionalProperties:false) — a misspelled filter
# key (e.g. "kee" instead of "key") would otherwise be silently ignored, turning a narrow
# filter into a match-all. manual/webhook are intentionally absent: the schema declares their
# config as an open object, so arbitrary keys are valid and must NOT be rejected.
_TRIGGER_CONFIG_KEYS = {
    "schedule": {"interval_minutes", "daily_time"},
    "workflow_run_completed": {"source_workflow_definition_id", "state"},
    "artifact_created": {"source_workflow_definition_id", "key", "kind"},
    "worker_status_changed": {"worker_id", "status"},
}
# Backstop against runaway event-driven automation (e.g. A->B->A completion-trigger loops).
MAX_TRIGGER_CHAIN_DEPTH = 20
LOGGER = logging.getLogger(__name__)


def _trigger_chain_blocks(target_workflow_id: Any, source_workflow_id: Any, chain: list[Any] | None) -> bool:
    """True if an event-driven trigger should be skipped to prevent runaway automation: the
    target is the source workflow (direct self-trigger), the target already appears in the
    chain of workflows that led here (a cycle such as A->B->A), or the chain is too deep."""
    chain = chain or []
    if target_workflow_id and target_workflow_id == source_workflow_id:
        return True
    if target_workflow_id and target_workflow_id in chain:
        return True
    return len(chain) >= MAX_TRIGGER_CHAIN_DEPTH


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
            if not isinstance(mode, str) or mode not in {"all", "any", "quorum"}:
                raise ValueError(f"workflow join node {node_id} mode must be all, any, or quorum")
            if mode == "quorum" and (not isinstance(node.get("quorum"), int) or node["quorum"] <= 0):
                raise ValueError(f"workflow join node {node_id} quorum must be a positive integer")
        if node["type"] == "human_gate" and "choices" in node:
            choices = node["choices"]
            if not isinstance(choices, list) or not choices:
                raise ValueError(f"workflow human_gate node {node_id} choices must be a non-empty list")
            choice_ids = []
            for choice in choices:
                if not isinstance(choice, dict) or not isinstance(choice.get("id"), str) or not choice["id"].strip():
                    raise ValueError(f"workflow human_gate node {node_id} choice requires id")
                if not isinstance(choice.get("label"), str) or not choice["label"].strip():
                    raise ValueError(f"workflow human_gate node {node_id} choice {choice['id']} requires label")
                choice_ids.append(choice["id"])
            if len(choice_ids) != len(set(choice_ids)):
                raise ValueError(f"workflow human_gate node {node_id} choice ids must be unique")
        if "budget_units" in node and (not isinstance(node["budget_units"], int) or node["budget_units"] <= 0):
            raise ValueError(f"workflow node {node_id} budget_units must be a positive integer")

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
        if condition.get("type") == "human_selected":
            source = node_map[edge["from"]]
            if source["type"] != "human_gate":
                raise ValueError(f"workflow edge at index {index} human_selected requires human_gate source")
            choices = {choice["id"] for choice in source.get("choices") or []}
            if condition.get("choice") not in choices:
                raise ValueError(f"workflow edge at index {index} human_selected choice is not declared by source gate")
        if node_map[edge["from"]]["type"] == "human_gate" and node_map[edge["from"]].get("choices") and condition.get("type") != "human_selected":
            raise ValueError(f"workflow human_gate edge at index {index} requires human_selected condition")

    incoming = {node_id: set() for node_id in node_ids}
    for edge in edges:
        incoming[edge["to"]].add(edge["from"])
    for node in nodes:
        if node.get("type") == "join" and node.get("mode", "all") == "quorum" and node["quorum"] > len(incoming[node["id"]]):
            raise ValueError(f"workflow join node {node['id']} quorum exceeds distinct incoming upstream count")

    if _has_cycle(node_ids, edges) and not _has_loop_guard(policy or {}, edges):
        raise ValueError("workflow graph has a cycle; policy.max_iterations or max_iterations_below is required")

    return graph


# Hard safety caps for workflow policy. Shared by the workflow API and pack import so
# neither path can persist a policy the other would reject. ponytail: one source.
WORKFLOW_POLICY_LIMITS = {
    "max_jobs": 100,
    "max_iterations": 100,
    "max_attempts_per_node": 25,
    "max_minutes": 1440,
    "requires_human_after_iterations": 100,
    "max_budget_units": 1000000,
}


def validate_workflow_policy(policy: dict[str, Any] | None) -> None:
    if policy is None:
        return
    if not isinstance(policy, dict):
        raise ValueError("workflow policy must be an object")
    for key, maximum in WORKFLOW_POLICY_LIMITS.items():
        if key not in policy:
            continue
        value = policy[key]
        if not isinstance(value, int) or value <= 0 or value > maximum:
            raise ValueError(f"workflow policy {key} must be an integer between 1 and {maximum}")
    if "stop_on_first_failure" in policy and not isinstance(policy["stop_on_first_failure"], bool):
        raise ValueError("workflow policy stop_on_first_failure must be boolean")


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
        if input is None:
            input = {}
        if not isinstance(input, dict):
            # Normalize only None -> {}; a falsy non-object ([], "", 0, False) must be rejected,
            # not silently coerced to an empty object.
            raise ValueError("workflow input must be an object")
        validate_workflow_graph(graph, policy)
        run = self._create_run(graph, policy, input, workflow_definition_id, definition.get("name") or "Workflow run")
        self._start_background(run["id"], graph, policy, input)
        return self.db.get_workflow_run(run["id"]) or run

    def _run_graph_policy(self, run: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Resolve the graph+policy a run must execute. Prefer the snapshot captured at run
        creation, so resume/recovery runs the SAME definition the run started on even after
        the live workflow_definition is edited or deleted. Fall back to the live definition
        only for legacy runs created before snapshots existed; raise if neither is available."""
        graph = run.get("graph_snapshot")
        if isinstance(graph, dict) and graph.get("nodes") is not None:
            policy = run.get("policy_snapshot")
            return graph, policy if isinstance(policy, dict) else {}
        definition = self.db.get_workflow_definition(run.get("workflow_definition_id") or "")
        if not definition:
            raise ValueError("workflow definition is unavailable; run cannot be resumed")
        return definition["graph"], definition.get("policy") or {}

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

    def resume_run(self, run_id: str, retry_interrupted: bool = False) -> dict[str, Any]:
        with self._thread_lock:
            run = self.db.get_workflow_run(run_id)
            if not run:
                raise ValueError(f"Unknown workflow_run_id: {run_id}")
            if run["state"] == "recovery_required":
                if not retry_interrupted:
                    raise ValueError("workflow run requires explicit retry_interrupted authorization")
                graph, policy = self._run_graph_policy(run)
                counters = run.get("counters") or {}
                completed = set(counters.get("completed_nodes") or [])
                recovery = counters.get("recovery") or {}
                interrupted = [item.get("node_key") for item in recovery.get("interrupted") or [] if item.get("node_key")]
                ready = []
                for node_key in [*interrupted, *(run.get("current_nodes") or [])]:
                    if node_key not in completed and node_key not in ready:
                        ready.append(node_key)
                if not ready:
                    raise ValueError("workflow run has no incomplete nodes to retry")
                recovery["retry_authorized_at"] = now_iso()
                counters["recovery"] = recovery
                self.db.update_workflow_run(run_id, state="running", current_nodes=ready, counters=counters, error=None, finished_at=None)
                self.db.append_workflow_event(run_id, "recovery_retry_authorized", {"nodes": ready})
                self.db.audit("workflow.recovery_retry_authorized", "workflow_run", run_id, {"nodes": ready})
                self._spawn_thread(run_id, graph, policy, run.get("input") or {})
                return self.db.get_workflow_run(run_id) or run
            if run["state"] != "paused":
                raise ValueError(f"workflow run {run_id} cannot be resumed from {run['state']}")
            graph, policy = self._run_graph_policy(run)
            self.db.update_workflow_run(run_id, state="running", error=None, finished_at=None)
            self.db.append_workflow_event(run_id, "run_resumed")
            active = self._threads.get(run_id)
            if not active or not active.is_alive():
                self._spawn_thread(run_id, graph, policy, run.get("input") or {})
        return self.db.get_workflow_run(run_id) or run

    def reconcile_runs(self) -> None:
        with self._thread_lock:
            for run in self.db.list_workflow_runs(limit=10000):
                if run["state"] in {"succeeded", "failed", "cancelled", "paused", "waiting_for_human", "recovery_required"}:
                    continue
                active = self._threads.get(run["id"])
                if active and active.is_alive():
                    continue
                try:
                    graph, policy = self._run_graph_policy(run)
                except ValueError:
                    self._mark_recovery_required(run, [], "workflow definition is unavailable")
                    continue
                node_map = {node["id"]: node for node in graph["nodes"]}
                completed = set((run.get("counters") or {}).get("completed_nodes") or [])
                runtime_nodes = self.db.list_workflow_nodes(run["id"])
                interrupted = []
                for runtime_node in runtime_nodes:
                    node = node_map.get(runtime_node["node_key"], {})
                    if runtime_node["state"] == "running" and node.get("type") in {"worker", "manager"}:
                        interrupted.append(
                            {
                                "workflow_node_id": runtime_node["id"],
                                "node_key": runtime_node["node_key"],
                                "job_id": runtime_node.get("job_id"),
                                "attempt": runtime_node.get("attempt"),
                            }
                        )
                        self.db.update_workflow_node(runtime_node["id"], state="interrupted", error="Atlas restarted during worker execution")
                for node_key in run.get("current_nodes") or []:
                    node = node_map.get(node_key, {})
                    if node_key in completed or node.get("type") not in {"worker", "manager"}:
                        continue
                    if not any(item["node_key"] == node_key for item in interrupted):
                        interrupted.append({"workflow_node_id": None, "node_key": node_key, "job_id": None, "attempt": None})
                if interrupted:
                    self._mark_recovery_required(run, interrupted)
                    continue
                ready = [node for node in run.get("current_nodes") or [] if node not in completed]
                if ready:
                    self.db.append_workflow_event(run["id"], "recovery_control_plane_resumed", {"nodes": ready})
                    self.db.audit("workflow.recovery_control_plane_resumed", "workflow_run", run["id"], {"nodes": ready})
                    self._spawn_thread(run["id"], graph, policy, run.get("input") or {})
                else:
                    counters = run.get("counters") or {}
                    self._finish_run(run["id"], "failed" if counters.get("failed_nodes") else "succeeded", counters, run.get("error"))

    def _mark_recovery_required(
        self,
        run: dict[str, Any],
        interrupted: list[dict[str, Any]],
        reason: str = "Atlas restarted while worker work may have been in progress",
    ) -> None:
        warning = "Retry may duplicate external worker side effects; verify the interrupted job before authorizing."
        counters = run.get("counters") or {}
        counters["recovery"] = {"interrupted": interrupted, "reason": reason, "warning": warning}
        self.db.update_workflow_run(run["id"], state="recovery_required", counters=counters, error=reason)
        payload = {"interrupted": interrupted, "reason": reason, "warning": warning}
        self.db.append_workflow_event(run["id"], "recovery_required", payload)
        self.db.audit("workflow.recovery_required", "workflow_run", run["id"], payload)

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        with self._thread_lock:
            run = self.db.get_workflow_run(run_id)
            if not run:
                raise ValueError(f"Unknown workflow_run_id: {run_id}")
            if run["state"] in {"succeeded", "failed", "cancelled"}:
                return run
            if not self.db.finalize_workflow_run(run_id, "cancelled", current_nodes=[], finished_at=now_iso()):
                # The runner thread finished the run between our read and this write — respect
                # its terminal state instead of clobbering it.
                return self.db.get_workflow_run(run_id) or run
            self.db.cancel_pending_approvals(run_id)
            self.db.append_workflow_event(run_id, "run_cancelled")
            self.db.append_workflow_event(run_id, "run_finished", {"state": "cancelled"})
        for node in self.db.list_workflow_nodes(run_id):
            if node["state"] == "running" and node.get("job_id"):
                self._cancel_job(node["job_id"])
        self._record_workflow_usage(run_id)
        self._notify_run_completed(run_id)
        return self.db.get_workflow_run(run_id) or run

    def approve_approval(self, approval_id: str) -> dict[str, Any]:
        with self._thread_lock:
            approval, run = self._pending_approval_context(approval_id)
            if approval.get("choices"):
                raise ValueError("approval requires a branch choice")
            graph, policy = self._run_graph_policy(run)
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
                self._continue_human_gate_decision(approval, run, graph, policy, runtime_node, None)
            else:
                counters["requires_human_after_iterations_approved"] = True
                self.db.update_workflow_run(run["id"], state="running", counters=counters, error=None, finished_at=None)
                active = self._threads.get(run["id"])
                if not active or not active.is_alive():
                    self._spawn_thread(run["id"], graph, policy, run.get("input") or {})
        return {"approval": approval, "run": self.db.get_workflow_run(run["id"]) or run}

    def choose_approval(self, approval_id: str, choice: str) -> dict[str, Any]:
        with self._thread_lock:
            approval, run = self._pending_approval_context(approval_id)
            if not approval.get("choices"):
                raise ValueError("approval does not declare branch choices")
            graph, policy = self._run_graph_policy(run)
            runtime_node = self.db.get_workflow_node(approval.get("workflow_node_id") or "")
            if not runtime_node or runtime_node["state"] != "waiting_for_human":
                raise ValueError("approval workflow node is unavailable")
            approval = self.db.choose_approval(approval_id, choice)
            self.db.append_workflow_event(
                run["id"], "approval_chosen", {"approval_id": approval_id, "choice": choice}, node_key=approval["node_key"]
            )
            self._continue_human_gate_decision(approval, run, graph, policy, runtime_node, choice)
        return {"approval": approval, "run": self.db.get_workflow_run(run["id"]) or run}

    def _continue_human_gate_decision(
        self,
        approval: dict[str, Any],
        run: dict[str, Any],
        graph: dict[str, Any],
        policy: dict[str, Any],
        runtime_node: dict[str, Any],
        choice: str | None,
    ) -> None:
        node_map = {node["id"]: node for node in graph["nodes"]}
        gate = node_map[approval["node_key"]]
        counters = run.get("counters") or {}
        completed_nodes = counters.setdefault("completed_nodes", [])
        completed = set(completed_nodes)
        if approval["node_key"] not in completed:
            completed.add(approval["node_key"])
            completed_nodes.append(approval["node_key"])
        ready = list(run.get("current_nodes") or [])
        join_states = counters.setdefault("join_states", _initial_join_states(graph))
        artifacts = self._load_artifacts(run["id"])
        cycle_edges = _cycle_edges(graph.get("edges", []))
        for edge in _outgoing_edges(graph.get("edges", [])).get(approval["node_key"], []):
            result = _evaluate_condition(edge.get("condition", {"type": "always"}), artifacts, counters, human_choice=choice)
            event_type = "edge_taken" if result["matched"] else "condition_skipped"
            self.db.append_workflow_event(
                run["id"], event_type, {"to": edge["to"], "condition_result": result}, node_key=approval["node_key"]
            )
            if result["matched"]:
                self.db.append_workflow_edge(run["id"], edge["from"], edge["to"], result)
                _schedule_node(edge, ready, completed, completed_nodes, node_map, join_states, cycle_edges)
        self.db.update_workflow_node(runtime_node["id"], state="succeeded", finished_at=now_iso())
        self.db.append_workflow_event(
            run["id"], "node_succeeded", {"approval_id": approval["id"], "choice": choice}, node_key=approval["node_key"]
        )
        self.db.update_workflow_run(run["id"], state="running", current_nodes=ready, counters=counters, error=None, finished_at=None)
        active = self._threads.get(run["id"])
        if not active or not active.is_alive():
            self._spawn_thread(run["id"], graph, policy, run.get("input") or {})

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
        if input is None:
            input = {}
        if not isinstance(input, dict):
            raise ValueError("workflow input must be an object")
        validate_workflow_graph(graph, policy)
        run = self._create_run(graph, policy, input, workflow_definition_id, name)
        return self._execute_run(run["id"], graph, policy, input)

    def _create_run(
        self,
        graph: dict[str, Any],
        policy: dict[str, Any],
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
                # Snapshot the graph+policy this run starts on, so resume/recovery executes
                # the same definition even if the live one is edited or deleted mid-run.
                "graph_snapshot": graph,
                "policy_snapshot": policy or {},
                "counters": {
                    "jobs_started": 0,
                    "budget_units_spent": 0,
                    "node_counts": {},
                    "completed_nodes": [],
                    "failed_nodes": [],
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
        context = contextvars.copy_context()
        thread = threading.Thread(
            target=context.run,
            args=(self._run_background, run_id, graph, policy, input),
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
        counters.setdefault("budget_units_spent", 0)
        counters.setdefault("node_counts", {})
        failed_nodes = counters.setdefault("failed_nodes", [])
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

        def record_join_failure(node_key: str) -> None:
            for join_key, state in join_states.items():
                if state.get("mode") != "quorum" or node_key not in state.get("upstream_nodes", []):
                    continue
                failed = state.setdefault("failed_upstreams", [])
                if node_key not in failed:
                    failed.append(node_key)
                    self.db.append_workflow_event(
                        run_id, "join_upstream_failed", {"join": join_key, "upstream": node_key}, node_key=join_key
                    )
                possible = len(state["upstream_nodes"]) - len(failed)
                if possible < state["quorum"] and len(state["completed_upstreams"]) < state["quorum"]:
                    state["state"] = "failed"
                    while join_key in ready:
                        ready.remove(join_key)
                    self.db.append_workflow_event(
                        run_id,
                        "join_quorum_impossible",
                        {
                            "quorum": state["quorum"],
                            "completed_upstreams": list(state["completed_upstreams"]),
                            "failed_upstreams": list(failed),
                        },
                        node_key=join_key,
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
                                    "choices": node.get("choices") or [],
                                }
                            )
                            self.db.update_workflow_node(runtime_node["id"], state="waiting_for_human")
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
                        record_join_failure(node_key)
                    if not runtime_node or policy.get("stop_on_first_failure", True):
                        raise
                    failure = {"node": node_key, "error": str(exc)}
                    if not any(item.get("node") == node_key for item in failed_nodes):
                        failed_nodes.append(failure)
                    counters["failure_summary"] = {
                        "count": len(failed_nodes),
                        "nodes": [item["node"] for item in failed_nodes],
                    }
                    self.db.append_workflow_event(run_id, "failure_recorded", failure, node_key=node_key)
                    self.db.update_workflow_run(run_id, current_nodes=ready, counters=counters)
                    continue

                schedule_outgoing(node_key, node, manager_decision)
                self.db.update_workflow_run(run_id, current_nodes=ready, counters=counters)

            if (self.db.get_workflow_run(run_id) or {}).get("state") == "cancelled":
                return self.db.get_workflow_run(run_id) or run
            if failed_nodes:
                summary = counters.get("failure_summary") or {"count": len(failed_nodes), "nodes": [item["node"] for item in failed_nodes]}
                self._finish_run(run_id, "failed", counters, f"workflow nodes failed: {', '.join(summary['nodes'])}")
            else:
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
            {
                "approval_id": approval["id"],
                "label": approval["label"],
                "reason": approval["reason"],
                "choices": approval.get("choices") or [],
            },
            node_key=approval["node_key"],
        )
        if self._threads.get(run_id) is threading.current_thread():
            self._threads.pop(run_id, None)

    def _finish_run(self, run_id: str, state: str, counters: dict[str, Any], error: str | None = None) -> None:
        won = self.db.finalize_workflow_run(
            run_id,
            state,
            current_nodes=[],
            counters=counters,
            error=error,
            finished_at=now_iso(),
        )
        if not won:
            # A concurrent cancel already moved the run to a terminal state; do not overwrite
            # it (cancelled -> succeeded) or double-emit run_finished / usage.
            return
        self.db.append_workflow_event(run_id, "run_finished", {"state": state, "error": error})
        self._record_workflow_usage(run_id)
        self._notify_run_completed(run_id)

    def _record_workflow_usage(self, run_id: str) -> None:
        try:
            run = self.db.get_workflow_run(run_id)
            if not run or run.get("state") not in {"succeeded", "failed", "cancelled"}:
                return
            counters = run.get("counters") or {}
            budget_units = int(counters.get("budget_units_spent") or 0)
            jobs = int(counters.get("jobs_started") or 0)
            seconds = elapsed_seconds(run.get("started_at"), run.get("finished_at"))
            self.db.emit_usage_event(
                {
                    "idempotency_key": f"run:{run_id}",
                    "kind": "workflow_run",
                    "run_id": run_id,
                    "status": run.get("state"),
                    "units": budget_units,
                    "seconds": seconds,
                    "started_at": run.get("started_at"),
                    "finished_at": run.get("finished_at"),
                    "metadata": {
                        "workflow_definition_id": run.get("workflow_definition_id"),
                        "measures": {
                            "workflow_run_count": 1,
                            "job_count": jobs,
                            "budget_units": budget_units,
                            "wall_seconds": seconds,
                        },
                        "billable": run.get("state") == "succeeded",
                        "billing_unit": "workflow_run",
                        "byok_token_counts_billable": False,
                    },
                }
            )
        except Exception:
            LOGGER.exception("usage metering failed for workflow run %s", run_id)

    def _notify_run_completed(self, run_id: str) -> None:
        if not self.trigger_service:
            return
        run = self.db.get_workflow_run(run_id)
        if run:
            # Carry the chain of workflows that led here (from this run's input) and append
            # this workflow, so fire_internal can reject cycles like A->B->A.
            chain = list((run.get("input") or {}).get("_trigger_chain") or [])
            wf_id = run.get("workflow_definition_id")
            if wf_id and wf_id not in chain:
                chain.append(wf_id)
            self.trigger_service.fire_internal(
                "workflow_run_completed",
                {
                    "run_id": run_id,
                    "workflow_definition_id": wf_id,
                    "state": run["state"],
                    "error": run.get("error"),
                    "_trigger_chain": chain,
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

        if not errors:
            combined_budget = sum(
                _node_budget_units(node_map[target])
                for target in selected_nodes
                if node_map[target].get("type") in {"worker", "manager"}
            )
            try:
                _check_budget(policy, int(counters.get("budget_units_spent") or 0), combined_budget)
            except _WorkflowGuardTripped as exc:
                errors.append(exc)
                for item in item_results:
                    if item["accepted"]:
                        item.update(accepted=False, reason=str(exc))

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
        payload = self._prepare_worker_node_payload(run, node, input, artifacts, policy, graph, counters)
        cost = _node_budget_units(node)
        _check_budget(policy, int(counters.get("budget_units_spent") or 0), cost)
        job = self.job_service.submit(payload)
        counters["budget_units_spent"] = int(counters.get("budget_units_spent") or 0) + cost
        self.db.update_workflow_run(run["id"], counters=counters)
        self.db.append_workflow_event(
            run["id"],
            "budget_reserved",
            {"job_id": job["id"], "budget_units": cost, "budget_units_spent": counters["budget_units_spent"]},
            node_key=node["id"],
        )
        return job

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
                # The job is still running remotely; cancel it before bailing so the worker
                # isn't left executing for a result no run will ever consume (matches the
                # run-cancel and max_minutes deadline paths above).
                self._cancel_job(job_id)
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
            result = self.fire_trigger(
                trigger["id"],
                payload={"trigger_id": trigger["id"], "scheduled_at": next_fire_at},
                dedupe_key=f"{trigger['id']}:{next_fire_at}",
            )
            # If the slot was already claimed — e.g. a crash after the dedupe claim but before
            # the run started and next_fire_at advanced — fire_trigger ignores it as a
            # duplicate and leaves next_fire_at pinned to this past slot, wedging the schedule
            # forever. Step it to the next future slot so the schedule keeps progressing.
            if result.get("run") is None and (result.get("event") or {}).get("state") == "ignored":
                advanced = next_fire_at_for_trigger(trigger, now)
                if advanced and _parse_utc(advanced) > now:
                    self.db.update_workflow_trigger(trigger["id"], {"next_fire_at": advanced})

    def fire_trigger(self, trigger_id: str, payload: dict[str, Any] | None = None, dedupe_key: str | None = None) -> dict[str, Any]:
        trigger = self.db.get_workflow_trigger(trigger_id)
        if not trigger:
            raise ValueError(f"Unknown workflow_trigger_id: {trigger_id}")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            # Normalize only None -> {}; a falsy non-object ([], "", 0, False) must be rejected,
            # not silently coerced, at this shared service boundary.
            raise ValueError("workflow trigger payload must be an object")

        if not trigger.get("enabled"):
            # A disabled trigger must not start a run even on the direct API fire path.
            # (The scheduler and internal event fan-out already pre-filter enabled=True;
            # this guards the one path that reaches fire_trigger without that filter.)
            event = self.db.append_workflow_trigger_event(
                trigger_id, "ignored", payload=payload, dedupe_key=dedupe_key, error="trigger disabled"
            )
            return {"trigger": trigger, "event": event, "run": None}

        if dedupe_key:
            # Atomically claim the key; a concurrent fire with the same key loses the claim
            # and is ignored (closes the check-then-insert race that could start two runs).
            if not self.db.claim_trigger_dedupe(trigger_id, dedupe_key, payload):
                event = self.db.append_workflow_trigger_event(trigger_id, "ignored", payload=payload, dedupe_key=dedupe_key, error="duplicate dedupe_key")
                return {"trigger": trigger, "event": event, "run": None}
        else:
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
        chain = list((run.get("input") or {}).get("_trigger_chain") or [])
        wf_id = run.get("workflow_definition_id")
        if wf_id and wf_id not in chain:
            chain.append(wf_id)
        return self.fire_internal(
            "artifact_created",
            {
                "artifact_id": artifact["id"],
                "run_id": artifact.get("run_id"),
                "workflow_definition_id": wf_id,
                "job_id": artifact.get("job_id"),
                "key": artifact["key"],
                "kind": artifact["kind"],
                "_trigger_chain": chain,
            },
            f"artifact_created:{artifact['id']}",
        )

    def fire_internal(self, event_type: str, payload: dict[str, Any], dedupe_key: str) -> list[dict[str, Any]]:
        results = []
        event_payload = {**payload, "event_type": event_type}
        chain = payload.get("_trigger_chain") or []
        try:
            triggers = self.db.list_workflow_triggers(limit=500, enabled=True)
            for trigger in triggers:
                if trigger.get("type") != event_type or not _event_trigger_matches(trigger, event_payload):
                    continue
                # Block direct self-triggering, longer cycles (A->B->A), and runaway depth —
                # otherwise event-driven triggers can loop forever.
                if event_type in {"workflow_run_completed", "artifact_created"} and _trigger_chain_blocks(
                    trigger["workflow_definition_id"], payload.get("workflow_definition_id"), chain
                ):
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
    config = payload.get("config")
    if config is not None and not isinstance(config, dict):
        raise ValueError("workflow trigger config must be an object")
    allowed = _TRIGGER_CONFIG_KEYS.get(trigger_type)
    if allowed is not None:
        # Only the closed-config types are checked; manual/webhook keep an open config (see
        # the schema). A typo'd filter key must fail loudly, not silently widen the trigger to
        # match every event (mirrors additionalProperties:false in the trigger schema).
        unknown = set(config or {}) - allowed
        if unknown:
            raise ValueError(f"unknown workflow trigger config key(s) for {trigger_type}: {', '.join(sorted(unknown))}")
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
    if condition_type == "human_selected":
        if not isinstance(condition.get("choice"), str) or not condition["choice"].strip():
            raise ValueError(f"workflow edge at index {edge_index} human_selected requires choice")
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
    states = {
        node["id"]: {
            "mode": node.get("mode") or "all",
            "state": "ready" if node["id"] == graph.get("start") else "waiting",
            "upstream_nodes": incoming.get(node["id"], []),
            "completed_upstreams": [],
        }
        for node in graph.get("nodes", [])
        if node.get("type") == "join"
    }
    for node in graph.get("nodes", []):
        if node.get("type") == "join" and node.get("mode") == "quorum":
            states[node["id"]]["quorum"] = node["quorum"]
            states[node["id"]]["failed_upstreams"] = []
    return states


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
            if "failed_upstreams" in join_states[target]:
                join_states[target]["failed_upstreams"] = []

    if node_map[target].get("type") == "join":
        state = join_states[target]
        if source not in state["completed_upstreams"]:
            state["completed_upstreams"].append(source)
        satisfied = (
            bool(state["completed_upstreams"])
            if state["mode"] == "any"
            else (
                len(state["completed_upstreams"]) >= state["quorum"]
                if state["mode"] == "quorum"
                else set(state["upstream_nodes"]) <= set(state["completed_upstreams"])
            )
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


def _node_budget_units(node: dict[str, Any]) -> int:
    if node.get("type") not in {"worker", "manager"}:
        return 0
    return int(node.get("budget_units", 1))


def _check_budget(policy: dict[str, Any], spent: int, additional: int) -> None:
    maximum = policy.get("max_budget_units")
    if isinstance(maximum, int) and maximum > 0 and spent + additional > maximum:
        raise _WorkflowGuardTripped(
            f"workflow policy max_budget_units exceeded: {spent} spent + {additional} requested > {maximum}"
        )


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
    human_choice: str | None = None,
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
    if condition_type == "human_selected":
        choice = condition.get("choice")
        return {"type": condition_type, "matched": choice == human_choice, "choice": choice}
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
