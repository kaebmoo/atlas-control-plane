from __future__ import annotations

import argparse
import json
import mimetypes
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import Config
from .db import ARTIFACT_KINDS, Database
from .jobs import JobManager, TERMINAL_STATES
from .router import Router
from .workflows import (
    WorkflowRunner,
    WorkflowTriggerService,
    next_fire_at_for_trigger,
    validate_workflow_graph,
    validate_workflow_trigger_payload,
)


STATIC_DIR = Path(__file__).parent / "static"
# ponytail: hard-coded safety caps; move to Config only when deployments need tuning.
_WORKFLOW_POLICY_LIMITS = {
    "max_jobs": 100,
    "max_iterations": 100,
    "max_attempts_per_node": 25,
    "max_minutes": 1440,
    "requires_human_after_iterations": 100,
}


class AtlasRuntime:
    def __init__(self, config: Config):
        self.config = config
        self.db = Database(config.db_path)
        self.jobs = JobManager(self.db, config.request_timeout_seconds)
        self.router = Router(self.db)
        self.workflows = WorkflowRunner(self.db, self.jobs)
        self.triggers = WorkflowTriggerService(self.db, self.workflows)


class AtlasHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], runtime: AtlasRuntime):
        super().__init__(server_address, AtlasHandler)
        self.runtime = runtime


class AtlasHandler(BaseHTTPRequestHandler):
    server: AtlasHttpServer
    protocol_version = "HTTP/1.1"

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path.startswith("/api/"):
                if not self._is_authorized():
                    self._json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
                    return
                self._handle_api(method, path, parse_qs(parsed.query))
                return
            self._handle_static(path)
        except ValueError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except FileNotFoundError:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except BrokenPipeError:
            return
        except Exception as exc:
            self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_api(self, method: str, path: str, query: dict[str, list[str]]) -> None:
        runtime = self.server.runtime
        parts = [part for part in path.split("/") if part]

        if method == "GET" and parts == ["api", "health"]:
            self._json(
                {
                    "ok": True,
                    "service": "atlas-control-plane",
                    "db": str(runtime.config.db_path),
                    "workers": len(runtime.db.list_workers()),
                }
            )
            return

        if parts == ["api", "workers"]:
            if method == "GET":
                self._json({"workers": [_public_worker(worker) for worker in runtime.db.list_workers()]})
                return
            if method == "POST":
                payload = self._read_json()
                worker = runtime.db.upsert_worker(payload)
                self._json({"worker": _public_worker(worker)}, HTTPStatus.CREATED)
                return

        if parts == ["api", "workers", "poll"] and method == "POST":
            self._json({"workers": [_public_worker(worker) for worker in runtime.jobs.poll_all_workers()]})
            return

        if len(parts) == 3 and parts[:2] == ["api", "workers"]:
            worker_id = parts[2]
            if method == "GET":
                worker = runtime.db.get_worker(worker_id)
                if not worker:
                    raise FileNotFoundError()
                self._json({"worker": _public_worker(worker)})
                return
            if method == "DELETE":
                if not runtime.db.delete_worker(worker_id):
                    raise FileNotFoundError()
                self._json({"deleted": True})
                return

        if len(parts) == 4 and parts[:2] == ["api", "workers"] and parts[3] == "poll" and method == "POST":
            self._json({"worker": _public_worker(runtime.jobs.poll_worker(parts[2]))})
            return

        if parts == ["api", "workspaces"]:
            if method == "GET":
                self._json({"workspaces": runtime.db.list_workspaces()})
                return
            if method == "POST":
                workspace = runtime.db.upsert_workspace(self._read_json())
                self._json({"workspace": workspace}, HTTPStatus.CREATED)
                return

        if len(parts) == 3 and parts[:2] == ["api", "workspaces"]:
            workspace_id = parts[2]
            if method == "GET":
                workspace = runtime.db.get_workspace(workspace_id)
                if not workspace:
                    raise FileNotFoundError()
                self._json({"workspace": workspace})
                return
            if method == "DELETE":
                if not runtime.db.delete_workspace(workspace_id):
                    raise FileNotFoundError()
                self._json({"deleted": True})
                return

        if parts == ["api", "conversations"]:
            if method == "GET":
                self._json({"conversations": runtime.db.list_conversations()})
                return
            if method == "POST":
                conversation = runtime.db.create_conversation(self._read_json())
                self._json({"conversation": conversation}, HTTPStatus.CREATED)
                return

        if parts == ["api", "routes", "resolve"] and method == "POST":
            decision = runtime.router.resolve(self._read_json())
            self._json(
                {
                    "worker": _public_worker(decision.worker),
                    "workspace": decision.workspace,
                    "reason": decision.reason,
                    "thclaws_session_id": decision.thclaws_session_id,
                }
            )
            return

        if parts == ["api", "jobs"]:
            if method == "GET":
                limit = int(query.get("limit", ["100"])[0])
                self._json({"jobs": runtime.db.list_jobs(limit)})
                return
            if method == "POST":
                job = runtime.jobs.submit(self._read_json())
                self._json({"job": job}, HTTPStatus.ACCEPTED)
                return

        if len(parts) == 3 and parts[:2] == ["api", "jobs"]:
            job = runtime.db.get_job(parts[2])
            if not job:
                raise FileNotFoundError()
            if method == "GET":
                self._json({"job": job})
                return

        if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "cancel" and method == "POST":
            self._json({"job": runtime.jobs.cancel(parts[2])})
            return

        if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "events" and method == "GET":
            after = int(query.get("after", ["0"])[0])
            self._stream_job_events(parts[2], after)
            return

        if parts == ["api", "workflows"]:
            if method == "GET":
                limit = int(query.get("limit", ["100"])[0])
                self._json({"workflows": runtime.db.list_workflow_definitions(limit)})
                return
            if method == "POST":
                payload = self._read_json()
                _validate_workflow_payload(runtime, payload)
                workflow = runtime.db.create_workflow_definition(payload)
                self._json({"workflow": workflow}, HTTPStatus.CREATED)
                return

        if parts == ["api", "workflows", "draft"] and method == "POST":
            self._json({"draft": _build_workflow_draft(runtime, self._read_json())}, HTTPStatus.CREATED)
            return

        if len(parts) == 3 and parts[:2] == ["api", "workflows"]:
            workflow_id = parts[2]
            workflow = runtime.db.get_workflow_definition(workflow_id)
            if not workflow:
                raise FileNotFoundError()
            if method == "GET":
                self._json({"workflow": workflow})
                return
            if method == "PUT":
                payload = self._read_json()
                graph = payload.get("graph", workflow["graph"])
                policy = payload.get("policy", workflow.get("policy") or {})
                _validate_workflow_payload(runtime, {"graph": graph, "policy": policy})
                updated = runtime.db.update_workflow_definition(workflow_id, payload)
                self._json({"workflow": updated})
                return
            if method == "DELETE":
                if not runtime.db.delete_workflow_definition(workflow_id):
                    raise FileNotFoundError()
                self._json({"deleted": True})
                return

        if len(parts) == 4 and parts[:2] == ["api", "workflows"] and parts[3] == "validate" and method == "POST":
            workflow = runtime.db.get_workflow_definition(parts[2])
            if not workflow:
                raise FileNotFoundError()
            payload = self._read_json()
            graph = payload.get("graph", workflow["graph"])
            policy = payload.get("policy", workflow.get("policy") or {})
            _validate_workflow_payload(runtime, {"graph": graph, "policy": policy})
            self._json({"ok": True})
            return

        if len(parts) == 4 and parts[:2] == ["api", "workflows"] and parts[3] == "explain" and method == "POST":
            workflow = runtime.db.get_workflow_definition(parts[2])
            if not workflow:
                raise FileNotFoundError()
            self._json({"explanation": _explain_workflow(workflow)})
            return

        if len(parts) == 4 and parts[:2] == ["api", "workflows"] and parts[3] == "repair" and method == "POST":
            workflow = runtime.db.get_workflow_definition(parts[2])
            if not workflow:
                raise FileNotFoundError()
            self._json({"draft": _repair_workflow(runtime, workflow, self._read_json())})
            return

        if parts == ["api", "workflow-runs"]:
            if method == "GET":
                limit = int(query.get("limit", ["100"])[0])
                workflow_definition_id = query.get("workflow_definition_id", [""])[0] or None
                self._json({"runs": runtime.db.list_workflow_runs(limit, workflow_definition_id)})
                return
            if method == "POST":
                payload = self._read_json()
                workflow_definition_id = payload.get("workflow_definition_id")
                if not workflow_definition_id:
                    raise ValueError("workflow_definition_id is required")
                run = runtime.workflows.start_workflow(workflow_definition_id, payload.get("input") or {})
                self._json({"run": run}, HTTPStatus.ACCEPTED)
                return

        if len(parts) == 3 and parts[:2] == ["api", "workflow-runs"] and method == "GET":
            run = runtime.db.get_workflow_run(parts[2])
            if not run:
                raise FileNotFoundError()
            self._json(
                {
                    "run": run,
                    "nodes": runtime.db.list_workflow_nodes(parts[2]),
                    "edges": runtime.db.list_workflow_edges(parts[2]),
                    "approvals": runtime.db.list_approvals(run_id=parts[2]),
                }
            )
            return

        if len(parts) == 4 and parts[:2] == ["api", "workflow-runs"] and parts[3] == "artifacts" and method == "GET":
            if not runtime.db.get_workflow_run(parts[2]):
                raise FileNotFoundError()
            self._json({"artifacts": [_public_artifact(artifact) for artifact in runtime.db.list_artifacts(run_id=parts[2])]})
            return

        if parts == ["api", "artifacts"] and method == "POST":
            payload = self._read_json()
            _validate_artifact_payload(runtime, payload)
            artifact = runtime.db.create_artifact(payload)
            runtime.triggers.artifact_created(artifact)
            self._json({"artifact": _public_artifact(artifact)}, HTTPStatus.CREATED)
            return

        if len(parts) == 3 and parts[:2] == ["api", "artifacts"] and method == "GET":
            artifact = runtime.db.get_artifact(parts[2])
            if not artifact:
                raise FileNotFoundError()
            self._json({"artifact": _public_artifact(artifact)})
            return

        if len(parts) == 4 and parts[:2] == ["api", "workflow-runs"] and parts[3] == "events" and method == "GET":
            if not runtime.db.get_workflow_run(parts[2]):
                raise FileNotFoundError()
            limit = int(query.get("limit", ["500"])[0])
            self._json({"events": runtime.db.list_workflow_events(parts[2], limit)})
            return

        if len(parts) == 4 and parts[:2] == ["api", "workflow-runs"] and method == "POST":
            action = parts[3]
            if action == "pause":
                self._json({"run": runtime.workflows.pause_run(parts[2])})
                return
            if action == "resume":
                self._json({"run": runtime.workflows.resume_run(parts[2])}, HTTPStatus.ACCEPTED)
                return
            if action == "cancel":
                self._json({"run": runtime.workflows.cancel_run(parts[2])})
                return

        if parts == ["api", "approvals"] and method == "GET":
            limit = int(query.get("limit", ["100"])[0])
            state = query.get("state", [""])[0] or None
            run_id = query.get("run_id", [""])[0] or None
            self._json({"approvals": runtime.db.list_approvals(limit, state, run_id)})
            return

        if len(parts) == 4 and parts[:2] == ["api", "approvals"] and method == "POST":
            if parts[3] == "approve":
                self._json(runtime.workflows.approve_approval(parts[2]), HTTPStatus.ACCEPTED)
                return
            if parts[3] == "reject":
                self._json(runtime.workflows.reject_approval(parts[2]))
                return

        if parts == ["api", "workflow-triggers"]:
            if method == "GET":
                limit = int(query.get("limit", ["100"])[0])
                workflow_definition_id = query.get("workflow_definition_id", [""])[0] or None
                self._json({"triggers": runtime.db.list_workflow_triggers(limit, workflow_definition_id)})
                return
            if method == "POST":
                payload = self._read_json()
                _prepare_workflow_trigger(runtime, payload)
                trigger = runtime.db.create_workflow_trigger(payload)
                self._json({"trigger": trigger}, HTTPStatus.CREATED)
                return

        if len(parts) == 4 and parts[:2] == ["api", "workflow-triggers"] and parts[3] == "fire" and method == "POST":
            trigger = runtime.db.get_workflow_trigger(parts[2])
            if trigger and trigger["type"] in {"workflow_run_completed", "artifact_created", "worker_status_changed"}:
                raise ValueError(f"{trigger['type']} triggers are fired by Atlas events")
            payload = self._read_json()
            result = runtime.triggers.fire_trigger(parts[2], payload.get("payload") or {}, payload.get("dedupe_key"))
            self._json(result, HTTPStatus.ACCEPTED)
            return

        if len(parts) == 4 and parts[:2] == ["api", "workflow-triggers"] and parts[3] == "events" and method == "GET":
            if not runtime.db.get_workflow_trigger(parts[2]):
                raise FileNotFoundError()
            limit = int(query.get("limit", ["100"])[0])
            self._json({"events": runtime.db.list_workflow_trigger_events(parts[2], limit)})
            return

        if len(parts) == 3 and parts[:2] == ["api", "workflow-triggers"]:
            trigger_id = parts[2]
            trigger = runtime.db.get_workflow_trigger(trigger_id)
            if not trigger:
                raise FileNotFoundError()
            if method == "GET":
                self._json({"trigger": trigger})
                return
            if method == "PUT":
                payload = self._read_json()
                merged = dict(trigger)
                merged.update(payload)
                if "type" in payload or "config" in payload:
                    merged.pop("next_fire_at", None)
                _prepare_workflow_trigger(runtime, merged)
                if "type" in payload or "config" in payload:
                    payload["next_fire_at"] = merged.get("next_fire_at")
                updated = runtime.db.update_workflow_trigger(trigger_id, payload)
                self._json({"trigger": updated})
                return
            if method == "DELETE":
                if not runtime.db.delete_workflow_trigger(trigger_id):
                    raise FileNotFoundError()
                self._json({"deleted": True})
                return

        if parts == ["api", "audit"] and method == "GET":
            limit = int(query.get("limit", ["100"])[0])
            self._json({"audit": runtime.db.list_audit(limit)})
            return

        raise FileNotFoundError()

    def _handle_static(self, path: str) -> None:
        if path in {"", "/"}:
            target = STATIC_DIR / "index.html"
        elif path.startswith("/static/"):
            target = STATIC_DIR / path.removeprefix("/static/")
        elif path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        else:
            target = STATIC_DIR / "index.html"
        target = target.resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.exists():
            raise FileNotFoundError()
        content = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self._cors_headers()
        self.send_header("Content-Type", mimetypes.guess_type(str(target))[0] or "application/octet-stream")
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _stream_job_events(self, job_id: str, after: int) -> None:
        runtime = self.server.runtime
        if not runtime.db.get_job(job_id):
            raise FileNotFoundError()
        self.send_response(HTTPStatus.OK)
        self._cors_headers()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        last_seq = after
        while True:
            rows = runtime.db.get_job_events_after(job_id, last_seq, limit=100)
            for row in rows:
                last_seq = int(row["seq"])
                payload = dict(row.get("payload") or {})
                payload.setdefault("seq", last_seq)
                payload.setdefault("created_at", row.get("created_at"))
                self._write_sse(row["event_type"], payload, last_seq)

            job = runtime.db.get_job(job_id)
            if not job or (job["state"] in TERMINAL_STATES and not rows):
                self._write_sse("close", {"state": job["state"] if job else "missing"}, last_seq + 1)
                self.close_connection = True
                break
            time.sleep(0.4)

    def _write_sse(self, event: str, payload: Any, event_id: int) -> None:
        data = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        self.wfile.write(f"id: {event_id}\n".encode("utf-8"))
        self.wfile.write(f"event: {event}\n".encode("utf-8"))
        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "authorization, content-type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")

    def _is_authorized(self) -> bool:
        token = self.server.runtime.config.api_token
        if not token:
            return True
        if self.server.runtime.config.enable_loopback_without_token and self.client_address[0] in {"127.0.0.1", "::1"}:
            return True
        query_token = parse_qs(urlparse(self.path).query).get("token", [None])[0]
        if query_token == token:
            return True
        return self.headers.get("Authorization") == f"Bearer {token}"


def run_server(config: Config) -> None:
    runtime = AtlasRuntime(config)
    server = AtlasHttpServer((config.host, config.port), runtime)
    runtime.triggers.start()
    print(f"Atlas listening on {config.base_url}")
    print(f"SQLite state: {config.db_path}")
    try:
        server.serve_forever()
    finally:
        runtime.triggers.stop()


def _public_worker(worker: dict[str, Any]) -> dict[str, Any]:
    public = dict(worker)
    token = public.pop("token", None)
    public["token_set"] = bool(token)
    return public


def _public_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    public = dict(artifact)
    if public.get("kind") == "json":
        public["content"] = json.loads(public.get("content") or "null")
    return public


def _validate_artifact_payload(runtime: AtlasRuntime, payload: dict[str, Any]) -> None:
    run_id = payload.get("run_id")
    if not run_id or not runtime.db.get_workflow_run(run_id):
        raise ValueError(f"Unknown workflow_run_id: {run_id}")
    if not isinstance(payload.get("kind", "text"), str) or payload.get("kind", "text") not in ARTIFACT_KINDS:
        raise ValueError(f"unsupported artifact kind: {payload.get('kind')}")
    if payload.get("job_id") and not runtime.db.get_job(payload["job_id"]):
        raise ValueError(f"Unknown job_id: {payload['job_id']}")
    if payload.get("metadata") is not None and not isinstance(payload.get("metadata"), dict):
        raise ValueError("artifact metadata must be an object")


def _validate_workflow_payload(runtime: AtlasRuntime, payload: dict[str, Any]) -> None:
    if "graph" not in payload:
        raise ValueError("workflow graph is required")
    graph = payload["graph"]
    policy = payload.get("policy") or {}
    validate_workflow_graph(graph, policy)
    _validate_workflow_references(runtime, graph, policy)
    _validate_workflow_policy(policy)
    _validate_workflow_draft_triggers(payload.get("triggers") or [])


def _validate_workflow_references(runtime: AtlasRuntime, graph: dict[str, Any], policy: dict[str, Any]) -> None:
    workers = {worker["id"]: worker for worker in runtime.db.list_workers()}
    workspaces = {workspace["id"]: workspace for workspace in runtime.db.list_workspaces()}
    allowed_worker_ids = set(_string_list(policy.get("allowed_worker_ids"), "policy.allowed_worker_ids"))
    allowed_workspace_ids = set(_string_list(policy.get("allowed_workspace_ids"), "policy.allowed_workspace_ids"))

    for node in graph.get("nodes") or []:
        node_id = node["id"]
        worker_id = node.get("worker_id")
        workspace_id = node.get("workspace_id")
        role = str(node.get("role") or "").strip().lower()

        if worker_id and worker_id not in workers:
            raise ValueError(f"workflow node {node_id} references unknown worker_id: {worker_id}")
        if worker_id and allowed_worker_ids and worker_id not in allowed_worker_ids:
            raise ValueError(f"workflow node {node_id} worker_id is not allowed by policy")

        if workspace_id:
            workspace = workspaces.get(workspace_id)
            if not workspace:
                raise ValueError(f"workflow node {node_id} references unknown workspace_id: {workspace_id}")
            if worker_id and workspace["worker_id"] != worker_id:
                raise ValueError(f"workflow node {node_id} workspace_id does not belong to worker_id")
            if allowed_workspace_ids and workspace_id not in allowed_workspace_ids:
                raise ValueError(f"workflow node {node_id} workspace_id is not allowed by policy")
            if allowed_worker_ids and workspace["worker_id"] not in allowed_worker_ids:
                raise ValueError(f"workflow node {node_id} workspace worker is not allowed by policy")

        if role and not worker_id and not workspace_id and not any(_worker_matches_role(worker, role) for worker in workers.values()):
            raise ValueError(f"workflow node {node_id} role has no matching worker: {role}")


def _validate_workflow_policy(policy: dict[str, Any]) -> None:
    for key, maximum in _WORKFLOW_POLICY_LIMITS.items():
        if key not in policy:
            continue
        value = policy[key]
        if not isinstance(value, int) or value <= 0 or value > maximum:
            raise ValueError(f"workflow policy {key} must be an integer between 1 and {maximum}")


def _validate_workflow_draft_triggers(triggers: Any) -> None:
    if not isinstance(triggers, list):
        raise ValueError("workflow draft triggers must be a list")
    for index, trigger in enumerate(triggers):
        if not isinstance(trigger, dict):
            raise ValueError(f"workflow draft trigger at index {index} must be an object")
        validate_workflow_trigger_payload(trigger)


def _string_list(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{name} must be a list of ids")
    return value


def _worker_matches_role(worker: dict[str, Any], role: str) -> bool:
    tags = {str(tag).strip().lower() for tag in worker.get("tags") or [] if str(tag).strip()}
    return str(worker.get("role") or "").lower() == role or role in tags


def _prepare_workflow_trigger(runtime: AtlasRuntime, payload: dict[str, Any]) -> None:
    workflow_definition_id = payload.get("workflow_definition_id")
    if not workflow_definition_id:
        raise ValueError("workflow_definition_id is required")
    if not runtime.db.get_workflow_definition(workflow_definition_id):
        raise ValueError(f"Unknown workflow_definition_id: {workflow_definition_id}")
    validate_workflow_trigger_payload(payload)
    if "next_fire_at" not in payload:
        payload["next_fire_at"] = next_fire_at_for_trigger(payload)


def _build_workflow_draft(runtime: AtlasRuntime, payload: dict[str, Any]) -> dict[str, Any]:
    plain_prompt = str(payload.get("plain_language_prompt") or "").strip()
    if not plain_prompt:
        raise ValueError("plain_language_prompt is required")
    draft = _run_workflow_builder(runtime, _builder_prompt(runtime, plain_prompt))
    _validate_workflow_payload(runtime, draft)
    draft.setdefault("triggers", [])
    draft.setdefault("warnings", [])
    return draft


def _repair_workflow(runtime: AtlasRuntime, workflow: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    graph = payload.get("graph", workflow["graph"])
    policy = payload.get("policy", workflow.get("policy") or {})
    try:
        _validate_workflow_payload(runtime, {"graph": graph, "policy": policy})
        return {
            "name": workflow.get("name") or "Workflow",
            "description": workflow.get("description") or "",
            "graph": graph,
            "policy": policy,
            "explanation": "Workflow already validates.",
            "warnings": [],
        }
    except ValueError as exc:
        prompt = _builder_prompt(runtime, f"Repair this workflow. Error: {exc}\nWorkflow JSON:\n{json.dumps({'graph': graph, 'policy': policy}, ensure_ascii=True)}")
        draft = _run_workflow_builder(runtime, prompt)
        _validate_workflow_payload(runtime, draft)
        return draft


def _run_workflow_builder(runtime: AtlasRuntime, prompt: str) -> dict[str, Any]:
    worker = _workflow_builder_worker(runtime.db.list_workers())
    if not worker:
        raise ValueError("No workflow_builder worker configured; add a worker with role or tag workflow_builder")
    job = runtime.jobs.submit({"worker_id": worker["id"], "prompt": prompt})
    result = runtime.workflows._wait_for_job(job["id"])
    if result["state"] != "succeeded":
        raise ValueError(f"workflow_builder job failed: {result.get('error') or result['state']}")
    return _json_from_text(result.get("assistant_text") or "")


def _builder_prompt(runtime: AtlasRuntime, plain_prompt: str) -> str:
    context = {
        "workers": [_public_worker(worker) for worker in runtime.db.list_workers()],
        "workspaces": runtime.db.list_workspaces(),
        "node_types": ["worker"],
        "condition_types": ["always", "artifact_equals", "artifact_in", "max_iterations_below"],
        "trigger_types": ["manual", "schedule"],
        "templates": [
            {
                "name": "reporter_anchor",
                "graph": {
                    "start": "reporter",
                    "nodes": [
                        {"id": "reporter", "type": "worker", "prompt": "Research {input.topic}", "outputs": ["notes"]},
                        {"id": "anchor", "type": "worker", "prompt": "Write from {artifact.notes}", "outputs": ["script"]},
                    ],
                    "edges": [{"from": "reporter", "to": "anchor", "condition": {"type": "always"}}],
                },
            }
        ],
    }
    return (
        "Return only a JSON object with keys name, description, graph, policy, triggers, explanation, warnings.\n"
        "Use graph nodes with id, type=worker, prompt, optional worker_id/workspace_id, outputs, output_format.\n"
        "Use edge conditions from the provided condition_types only.\n\n"
        f"Context JSON:\n{json.dumps(context, ensure_ascii=True)}\n\n"
        f"User request:\n{plain_prompt}"
    )


def _workflow_builder_worker(workers: list[dict[str, Any]]) -> dict[str, Any] | None:
    for worker in workers:
        tags = worker.get("tags") or []
        if worker.get("role") == "workflow_builder" or "workflow_builder" in tags:
            return worker
    return None


def _json_from_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        raise ValueError("workflow_builder response did not contain JSON")
    data = json.loads(stripped[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("workflow_builder response must be a JSON object")
    return data


def _explain_workflow(workflow: dict[str, Any]) -> str:
    graph = workflow.get("graph") or {}
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    node_names = ", ".join(node.get("id", "?") for node in nodes)
    return (
        f"{workflow.get('name') or 'Workflow'} starts at {graph.get('start')}. "
        f"It has {len(nodes)} node(s): {node_names}. "
        f"It uses {len(edges)} edge(s) and policy {json.dumps(workflow.get('policy') or {}, ensure_ascii=True)}."
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Atlas control plane")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--db", default=None)
    args = parser.parse_args(argv)

    config = Config.from_env()
    if args.host or args.port or args.db:
        config = Config(
            host=args.host or config.host,
            port=args.port or config.port,
            db_path=Path(args.db).resolve() if args.db else config.db_path,
            api_token=config.api_token,
            request_timeout_seconds=config.request_timeout_seconds,
            enable_loopback_without_token=config.enable_loopback_without_token,
        )
    run_server(config)
