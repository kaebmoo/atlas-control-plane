from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import re
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from .config import Config
from .db import ARTIFACT_KINDS, Database, new_id
from .jobs import JobManager, TERMINAL_STATES
from .router import Router
from .usage import normalize_usage_range, summarize_usage, usage_csv
from .workflow_templates import workflow_templates
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
    "max_budget_units": 1000000,
}
_WORKFLOW_POLICY_DEFAULTS = {
    "max_jobs": 20,
    "max_iterations": 5,
    "max_attempts_per_node": 3,
    "max_minutes": 30,
    "stop_on_first_failure": True,
}
ROLE_PERMISSIONS = {
    "admin": frozenset({"read", "audit.read", "jobs.run", "workflows.run", "approvals.decide", "workflows.manage", "workers.poll", "resources.manage", "admin"}),
    "operator": frozenset({"read", "jobs.run", "workflows.run", "approvals.decide", "workflows.manage", "workers.poll", "resources.manage"}),
    "viewer": frozenset({"read"}),
    "auditor": frozenset({"read", "audit.read"}),
}


class AtlasRuntime:
    def __init__(self, config: Config):
        self.config = config
        self.db = Database(config.db_path, secret_key=config.secret_key)
        self.upload_dir = (config.upload_dir or config.db_path.parent / "uploads").resolve()
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.jobs = JobManager(self.db, config.request_timeout_seconds)
        self.router = Router(self.db)
        self.workflows = WorkflowRunner(self.db, self.jobs)
        self.triggers = WorkflowTriggerService(self.db, self.workflows)
        self.workflows.reconcile_runs()


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
                parts = [part for part in path.split("/") if part]
                if method == "POST" and parts == ["api", "auth", "login"]:
                    self._handle_api(method, path, parse_qs(parsed.query))
                    return
                if not self._is_authorized():
                    self._json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
                    return
                permission = _required_permission(method, parts)
                if permission not in ROLE_PERMISSIONS.get(self.auth_identity["role"], frozenset()):
                    self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
                    return
                with self.server.runtime.db.as_actor(self.auth_identity["username"]):
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

        if parts == ["api", "auth", "login"] and method == "POST":
            payload = self._read_json()
            username = str(payload.get("username") or "").strip()
            password = str(payload.get("password") or "")
            user = runtime.db.verify_user_password(username, password)
            if not user:
                self._json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
                return
            with runtime.db.as_actor(user["username"]):
                token, raw_token = runtime.db.create_api_token(user["id"], "dashboard login")
                runtime.db.audit("auth.login", "user", user["id"], {"token_id": token["id"]})
            self._json({"token": raw_token, "user": user})
            return

        if parts == ["api", "auth", "logout"] and method == "POST":
            token_id = self.auth_identity.get("token_id")
            if token_id:
                runtime.db.revoke_api_token(token_id)
            runtime.db.audit("auth.logout", "user", self.auth_identity.get("id") or "legacy")
            self._json({"logged_out": True})
            return

        if parts == ["api", "me"] and method == "GET":
            self._json({"user": {key: self.auth_identity.get(key) for key in ("id", "username", "role", "status")}})
            return

        if parts == ["api", "users"]:
            if method == "GET":
                self._json({"users": runtime.db.list_users()})
                return
            if method == "POST":
                payload = self._read_json()
                user = runtime.db.create_user(
                    str(payload.get("username") or ""),
                    str(payload.get("password") or ""),
                    str(payload.get("role") or "viewer"),
                    str(payload.get("status") or "active"),
                )
                self._json({"user": user}, HTTPStatus.CREATED)
                return

        if len(parts) == 3 and parts[:2] == ["api", "users"]:
            user_id = parts[2]
            if method == "GET":
                user = runtime.db.get_user(user_id)
                if not user:
                    raise FileNotFoundError()
                self._json({"user": user})
                return
            if method == "PUT":
                user = runtime.db.update_user(user_id, self._read_json())
                if not user:
                    raise FileNotFoundError()
                self._json({"user": user})
                return
            if method == "DELETE":
                if not runtime.db.delete_user(user_id):
                    raise FileNotFoundError()
                self._json({"deleted": True})
                return

        if parts == ["api", "tokens"]:
            if method == "GET":
                user_id = query.get("user_id", [""])[0] or None
                self._json({"tokens": runtime.db.list_api_tokens(user_id)})
                return
            if method == "POST":
                payload = self._read_json()
                user_id = str(payload.get("user_id") or "")
                if not user_id and payload.get("username"):
                    user = runtime.db.get_user_by_username(str(payload["username"]))
                    user_id = user["id"] if user else ""
                token, raw_token = runtime.db.create_api_token(user_id, str(payload.get("name") or "api"))
                self._json({"token": token, "api_token": raw_token}, HTTPStatus.CREATED)
                return

        if len(parts) == 3 and parts[:2] == ["api", "tokens"]:
            token_id = parts[2]
            if method == "GET":
                token = runtime.db.get_api_token(token_id)
                if not token:
                    raise FileNotFoundError()
                self._json({"token": token})
                return
            if method == "PUT":
                token = runtime.db.update_api_token(token_id, str(self._read_json().get("name") or ""))
                if not token:
                    raise FileNotFoundError()
                self._json({"token": token})
                return
            if method == "DELETE":
                if not runtime.db.revoke_api_token(token_id):
                    raise FileNotFoundError()
                self._json({"revoked": True})
                return

        if len(parts) == 4 and parts[:2] == ["api", "tokens"] and parts[3] == "revoke" and method == "POST":
            if not runtime.db.revoke_api_token(parts[2]):
                raise FileNotFoundError()
            self._json({"revoked": True})
            return

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

        if method == "GET" and parts == ["api", "usage"]:
            from_at, to_at = normalize_usage_range(
                query.get("from", [""])[0] or None,
                query.get("to", [""])[0] or None,
            )
            events = runtime.db.list_usage_events(from_at, to_at)
            output_format = query.get("format", ["json"])[0].lower()
            if output_format == "json":
                self._json({"usage": events, "totals": summarize_usage(events), "from": from_at, "to": to_at})
                return
            if output_format == "csv":
                self._csv(usage_csv(events))
                return
            raise ValueError("usage format must be json or csv")

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

        if parts == ["api", "workflow-templates"] and method == "GET":
            self._json({"templates": workflow_templates()})
            return

        if parts == ["api", "workflows", "draft"] and method == "POST":
            self._json({"draft": _build_workflow_draft(runtime, self._read_json())}, HTTPStatus.CREATED)
            return

        if parts == ["api", "workflows", "suggest-workers"] and method == "POST":
            self._json({"suggestions": _suggest_workflow_workers(runtime, self._read_json())})
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
            self._json({"explanation": _explain_workflow(runtime, workflow)})
            return

        if len(parts) == 4 and parts[:2] == ["api", "workflows"] and parts[3] == "repair" and method == "POST":
            workflow = runtime.db.get_workflow_definition(parts[2])
            if not workflow:
                raise FileNotFoundError()
            self._json({"draft": _repair_workflow(runtime, workflow, self._read_json())})
            return

        if len(parts) == 4 and parts[:2] == ["api", "workflows"] and parts[3] == "suggest-triggers" and method == "POST":
            workflow = runtime.db.get_workflow_definition(parts[2])
            if not workflow:
                raise FileNotFoundError()
            self._json({"triggers": _suggest_workflow_triggers(runtime, workflow, self._read_json())})
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

        if len(parts) == 4 and parts[:2] == ["api", "workflow-runs"] and parts[3] == "files" and method == "POST":
            artifact = self._upload_workflow_file(parts[2], query)
            runtime.triggers.artifact_created(artifact)
            self._json({"artifact": _public_artifact(artifact)}, HTTPStatus.CREATED)
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

        if len(parts) == 4 and parts[:2] == ["api", "artifacts"] and parts[3] == "content" and method == "GET":
            self._download_artifact(parts[2])
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
                payload = self._read_json()
                self._json(
                    {"run": runtime.workflows.resume_run(parts[2], retry_interrupted=payload.get("retry_interrupted") is True)},
                    HTTPStatus.ACCEPTED,
                )
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
            if parts[3] == "choose":
                choice = self._read_json().get("choice")
                if not isinstance(choice, str) or not choice:
                    raise ValueError("choice is required")
                self._json(runtime.workflows.choose_approval(parts[2], choice), HTTPStatus.ACCEPTED)
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

    def _upload_workflow_file(self, run_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
        runtime = self.server.runtime
        if not runtime.db.get_workflow_run(run_id):
            raise ValueError(f"Unknown workflow_run_id: {run_id}")
        key = query.get("key", [""])[0]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}", key):
            raise ValueError("file artifact key is invalid")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Content-Length must be an integer") from exc
        if length < 0 or length > runtime.config.max_upload_bytes:
            raise ValueError(f"file upload exceeds maximum of {runtime.config.max_upload_bytes} bytes")
        filename = _safe_download_filename(self.headers.get("X-Filename") or "upload.bin")
        media_type = (self.headers.get("Content-Type") or "application/octet-stream").split(";", 1)[0].strip()
        opaque_id = new_id("file")
        target = runtime.upload_dir / opaque_id
        temporary = runtime.upload_dir / f".{opaque_id}.tmp"
        digest = hashlib.sha256()
        remaining = length
        try:
            with temporary.open("xb") as output:
                while remaining:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        raise ValueError("file upload body is incomplete")
                    output.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
            artifact = runtime.db.create_artifact(
                {
                    "run_id": run_id,
                    "key": key,
                    "kind": "file_ref",
                    "content": opaque_id,
                    "metadata": {
                        "filename": filename,
                        "media_type": media_type or "application/octet-stream",
                        "size": length,
                        "sha256": digest.hexdigest(),
                    },
                }
            )
            return artifact
        except Exception:
            temporary.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise

    def _download_artifact(self, artifact_id: str) -> None:
        runtime = self.server.runtime
        artifact = runtime.db.get_artifact(artifact_id)
        if not artifact:
            raise FileNotFoundError()
        if artifact.get("kind") != "file_ref":
            raise ValueError("artifact is not a file_ref")
        root = runtime.upload_dir.resolve()
        target = (root / str(artifact.get("content") or "")).resolve()
        if target.parent != root or not target.is_file():
            raise ValueError("file_ref is outside the upload root or missing")
        content = target.read_bytes()
        metadata = artifact.get("metadata") or {}
        filename = _safe_download_filename(metadata.get("filename") or "download.bin")
        disposition = f"attachment; filename=\"download\"; filename*=UTF-8''{quote(filename, safe='')}"
        self.send_response(HTTPStatus.OK)
        self._cors_headers()
        self.send_header("Content-Type", metadata.get("media_type") or "application/octet-stream")
        self.send_header("Content-Disposition", disposition)
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

    def _csv(self, content: str) -> None:
        body = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self._cors_headers()
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="atlas-usage.csv"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "authorization, content-type, x-filename")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")

    def _is_authorized(self) -> bool:
        runtime = self.server.runtime
        self.auth_identity = {}
        if runtime.config.enable_loopback_without_token and self.client_address[0] in {"127.0.0.1", "::1"}:
            self.auth_identity = {"id": None, "username": "local", "role": "admin", "status": "active", "token_id": None}
            return True
        authorization = self.headers.get("Authorization") or ""
        bearer_token = authorization[7:] if authorization.startswith("Bearer ") else None
        raw_token = bearer_token or parse_qs(urlparse(self.path).query).get("token", [None])[0]
        if not raw_token:
            return False
        legacy_token = runtime.config.api_token
        if legacy_token and hmac.compare_digest(raw_token, legacy_token):
            self.auth_identity = {"id": None, "username": "legacy", "role": "admin", "status": "active", "token_id": None}
            return True
        identity = runtime.db.authenticate_api_token(raw_token)
        if not identity:
            return False
        self.auth_identity = identity
        return True


def _required_permission(method: str, parts: list[str]) -> str:
    if parts[:2] in (["api", "users"], ["api", "tokens"]):
        return "admin"
    if parts == ["api", "audit"]:
        return "audit.read"
    if parts == ["api", "usage"]:
        return "audit.read"
    if method == "GET" or parts in (["api", "me"], ["api", "auth", "logout"]):
        return "read"
    if parts[:2] == ["api", "jobs"] or parts == ["api", "routes", "resolve"]:
        return "jobs.run"
    if parts[:2] == ["api", "approvals"]:
        return "approvals.decide"
    if parts[:2] == ["api", "workflow-runs"] or parts[:2] == ["api", "artifacts"]:
        return "workflows.run"
    if parts[:2] == ["api", "workflow-triggers"]:
        return "workflows.run" if len(parts) == 4 and parts[3] == "fire" else "workflows.manage"
    if parts[:2] == ["api", "workflows"]:
        return "workflows.manage"
    if parts[:2] == ["api", "workers"]:
        if method == "POST" and (parts == ["api", "workers", "poll"] or (len(parts) == 4 and parts[3] == "poll")):
            return "workers.poll"
        return "admin"
    return "resources.manage"


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


def _safe_download_filename(value: Any) -> str:
    filename = Path(str(value or "download.bin").replace("\\", "/")).name
    filename = filename.replace("\r", "").replace("\n", "").replace('"', "").strip()
    return filename[:255] or "download.bin"


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


def _validate_workflow_references(
    runtime: AtlasRuntime,
    graph: dict[str, Any],
    policy: dict[str, Any],
    allow_unresolved_roles: bool = False,
) -> None:
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

        if (
            role
            and not worker_id
            and not workspace_id
            and not allow_unresolved_roles
            and not any(_worker_matches_role(worker, role) for worker in workers.values())
        ):
            raise ValueError(f"workflow node {node_id} role has no matching worker: {role}")


def _validate_workflow_policy(policy: dict[str, Any]) -> None:
    for key, maximum in _WORKFLOW_POLICY_LIMITS.items():
        if key not in policy:
            continue
        value = policy[key]
        if not isinstance(value, int) or value <= 0 or value > maximum:
            raise ValueError(f"workflow policy {key} must be an integer between 1 and {maximum}")
    if "stop_on_first_failure" in policy and not isinstance(policy["stop_on_first_failure"], bool):
        raise ValueError("workflow policy stop_on_first_failure must be boolean")


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
    triggers = payload.get("triggers") or []
    try:
        _validate_workflow_payload(runtime, {"graph": graph, "policy": policy, "triggers": triggers})
        return {
            "name": workflow.get("name") or "Workflow",
            "description": workflow.get("description") or "",
            "graph": graph,
            "policy": policy,
            "triggers": triggers,
            "explanation": "Workflow already validates.",
            "warnings": [],
        }
    except ValueError as exc:
        prompt = _builder_prompt(
            runtime,
            f"Repair this workflow. Error: {exc}\nWorkflow JSON:\n"
            f"{json.dumps({'graph': graph, 'policy': policy, 'triggers': triggers}, ensure_ascii=True)}",
        )
        draft = _run_workflow_builder(runtime, prompt)
        _validate_workflow_payload(runtime, draft)
        return draft


def _suggest_workflow_triggers(
    runtime: AtlasRuntime,
    workflow: dict[str, Any],
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    intent = str(payload.get("plain_language_prompt") or "Suggest suitable triggers for this workflow.").strip()
    result = _run_workflow_builder(
        runtime,
        "Return only a JSON object with one key, triggers, containing trigger drafts. "
        "Each draft may use only type, name, config, and enabled.\n\n"
        f"Builder context JSON:\n{json.dumps(_builder_context(runtime), ensure_ascii=True)}\n\n"
        f"Workflow JSON:\n{json.dumps(workflow, ensure_ascii=True)}\n\n"
        f"User request:\n{intent}",
    )
    triggers = result.get("triggers")
    _validate_workflow_draft_triggers(triggers)
    return triggers


def _suggest_workflow_workers(runtime: AtlasRuntime, payload: dict[str, Any]) -> list[dict[str, Any]]:
    graph = payload.get("graph")
    policy = payload.get("policy") or {}
    validate_workflow_graph(graph, policy)
    _validate_workflow_policy(policy)
    _validate_workflow_references(runtime, graph, policy, allow_unresolved_roles=True)
    unresolved = [
        node for node in graph["nodes"]
        if node.get("type") in {"worker", "manager"} and not node.get("worker_id") and not node.get("workspace_id")
    ]
    if not unresolved:
        return []
    if _workflow_builder_worker(runtime.db.list_workers()):
        result = _run_workflow_builder(
            runtime,
            "Return only a JSON object with key suggestions. Return exactly one item for each unresolved node. "
            "Each item has node_id, role, optional worker_id, optional workspace_id, reason, and state "
            "(matched, fallback, or unavailable). Do not invent ids.\n\n"
            f"Builder context JSON:\n{json.dumps(_builder_context(runtime), ensure_ascii=True)}\n\n"
            f"Graph JSON:\n{json.dumps(graph, ensure_ascii=True)}\n\n"
            f"Policy JSON:\n{json.dumps(policy, ensure_ascii=True)}",
        )
        suggestions = result.get("suggestions")
    else:
        suggestions = _local_worker_suggestions(runtime, unresolved, policy)
    return _validate_worker_suggestions(runtime, unresolved, policy, suggestions)


def _local_worker_suggestions(
    runtime: AtlasRuntime,
    unresolved: list[dict[str, Any]],
    policy: dict[str, Any],
) -> list[dict[str, Any]]:
    allowed_workers = set(_string_list(policy.get("allowed_worker_ids"), "policy.allowed_worker_ids"))
    workers = sorted(runtime.db.list_workers(), key=lambda worker: worker["id"])
    if allowed_workers:
        workers = [worker for worker in workers if worker["id"] in allowed_workers]
    suggestions = []
    for node in unresolved:
        role = str(node.get("role") or "").strip().lower()
        match = next((worker for worker in workers if role and _worker_matches_role(worker, role)), None)
        if match:
            suggestions.append(
                {
                    "node_id": node["id"],
                    "role": role,
                    "worker_id": match["id"],
                    "reason": f"Exact role/tag match for {role}",
                    "state": "matched",
                }
            )
        else:
            suggestions.append(
                {
                    "node_id": node["id"],
                    "role": role,
                    "reason": f"No configured worker matches role {role or '(missing)'}; configure a worker or apply an explicit id",
                    "state": "unavailable",
                }
            )
    return suggestions


def _validate_worker_suggestions(
    runtime: AtlasRuntime,
    unresolved: list[dict[str, Any]],
    policy: dict[str, Any],
    suggestions: Any,
) -> list[dict[str, Any]]:
    if not isinstance(suggestions, list):
        raise ValueError("workflow_builder suggestions must be a list")
    expected = {node["id"]: str(node.get("role") or "").strip().lower() for node in unresolved}
    workers = {worker["id"]: worker for worker in runtime.db.list_workers()}
    workspaces = {workspace["id"]: workspace for workspace in runtime.db.list_workspaces()}
    allowed_workers = set(_string_list(policy.get("allowed_worker_ids"), "policy.allowed_worker_ids"))
    allowed_workspaces = set(_string_list(policy.get("allowed_workspace_ids"), "policy.allowed_workspace_ids"))
    normalized = []
    seen = set()
    for item in suggestions:
        if not isinstance(item, dict):
            raise ValueError("workflow_builder suggestion items must be objects")
        node_id = item.get("node_id")
        if node_id not in expected or node_id in seen:
            raise ValueError(f"workflow_builder suggestion references unexpected node_id: {node_id}")
        seen.add(node_id)
        role = str(item.get("role") or "").strip().lower()
        if role != expected[node_id]:
            raise ValueError(f"workflow_builder suggestion role does not match node {node_id}")
        state = item.get("state")
        if state not in {"matched", "fallback", "unavailable"}:
            raise ValueError(f"workflow_builder suggestion for {node_id} has invalid state")
        worker_id = item.get("worker_id") or None
        workspace_id = item.get("workspace_id") or None
        if worker_id and worker_id not in workers:
            raise ValueError(f"workflow_builder invented worker_id: {worker_id}")
        if worker_id and allowed_workers and worker_id not in allowed_workers:
            raise ValueError(f"workflow_builder suggested policy-forbidden worker_id: {worker_id}")
        if workspace_id:
            workspace = workspaces.get(workspace_id)
            if not workspace:
                raise ValueError(f"workflow_builder invented workspace_id: {workspace_id}")
            if worker_id and workspace["worker_id"] != worker_id:
                raise ValueError(f"workflow_builder workspace_id does not belong to worker_id for {node_id}")
            if allowed_workspaces and workspace_id not in allowed_workspaces:
                raise ValueError(f"workflow_builder suggested policy-forbidden workspace_id: {workspace_id}")
            if allowed_workers and workspace["worker_id"] not in allowed_workers:
                raise ValueError(f"workflow_builder suggested workspace owned by a forbidden worker for {node_id}")
        if state != "unavailable" and not worker_id and not workspace_id:
            raise ValueError(f"workflow_builder suggestion for {node_id} must include a worker_id or workspace_id")
        normalized.append(
            {
                "node_id": node_id,
                "role": role,
                **({"worker_id": worker_id} if worker_id else {}),
                **({"workspace_id": workspace_id} if workspace_id else {}),
                "reason": str(item.get("reason") or ""),
                "state": state,
            }
        )
    if seen != set(expected):
        raise ValueError("workflow_builder must return exactly one suggestion per unresolved node")
    return normalized


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
    return (
        "Return only a JSON object with keys name, description, graph, policy, triggers, explanation, warnings.\n"
        "Use only the node, condition, trigger, artifact, and policy contracts in the context.\n"
        "Manager nodes must use schema=manager_decision_v1 and manager_selected outgoing edges.\n"
        "Cycles must be bounded by policy.max_iterations or max_iterations_below.\n\n"
        f"Context JSON:\n{json.dumps(_builder_context(runtime), ensure_ascii=True)}\n\n"
        f"User request:\n{plain_prompt}"
    )


def _builder_context(runtime: AtlasRuntime) -> dict[str, Any]:
    return {
        "workers": [_public_worker(worker) for worker in runtime.db.list_workers()],
        "workspaces": runtime.db.list_workspaces(),
        "node_types": {
            "worker": {"fields": ["id", "prompt", "worker_id", "workspace_id", "role", "outputs", "output_format", "budget_units"]},
            "manager": {"fields": ["id", "prompt", "worker_id", "workspace_id", "role", "schema", "budget_units"], "schema": "manager_decision_v1"},
            "join": {"fields": ["id", "mode", "quorum"], "modes": ["all", "any", "quorum"]},
            "human_gate": {"fields": ["id", "label", "reason", "choices"]},
        },
        "condition_types": ["always", "artifact_equals", "artifact_in", "manager_selected", "human_selected", "max_iterations_below"],
        "trigger_types": ["manual", "schedule", "webhook", "workflow_run_completed", "artifact_created", "worker_status_changed"],
        "schedule_configs": [{"interval_minutes": 15}, {"daily_time": "09:30"}],
        "artifact_kinds": sorted(ARTIFACT_KINDS),
        "policy_defaults": _WORKFLOW_POLICY_DEFAULTS,
        "policy_limits": _WORKFLOW_POLICY_LIMITS,
        "templates": workflow_templates(),
    }


def _workflow_builder_worker(workers: list[dict[str, Any]]) -> dict[str, Any] | None:
    for worker in workers:
        tags = worker.get("tags") or []
        if worker.get("role") == "workflow_builder" or "workflow_builder" in tags:
            return worker
    return None


def _json_from_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"workflow_builder response must be one JSON object: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise ValueError("workflow_builder response must be a JSON object")
    return data


def _explain_workflow(runtime: AtlasRuntime, workflow: dict[str, Any]) -> str:
    if _workflow_builder_worker(runtime.db.list_workers()):
        result = _run_workflow_builder(
            runtime,
            "Return only a JSON object with one string key named explanation. Explain this validated workflow plainly.\n\n"
            f"Workflow JSON:\n{json.dumps(workflow, ensure_ascii=True)}",
        )
        explanation = result.get("explanation")
        if not isinstance(explanation, str) or not explanation.strip():
            raise ValueError("workflow_builder explanation must be a non-empty string")
        return explanation.strip()
    return _local_workflow_explanation(workflow)


def _local_workflow_explanation(workflow: dict[str, Any]) -> str:
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
        db_path = Path(args.db).resolve() if args.db else config.db_path
        upload_dir = db_path.parent / "uploads" if args.db and "ATLAS_UPLOAD_DIR" not in os.environ else config.upload_dir
        config = Config(
            host=args.host or config.host,
            port=args.port or config.port,
            db_path=db_path,
            api_token=config.api_token,
            request_timeout_seconds=config.request_timeout_seconds,
            enable_loopback_without_token=config.enable_loopback_without_token,
            secret_key=config.secret_key,
            upload_dir=upload_dir,
            max_upload_bytes=config.max_upload_bytes,
        )
    run_server(config)
