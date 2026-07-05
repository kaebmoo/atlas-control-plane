from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import re
import sys
import threading
import time
from dataclasses import replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, quote, urlparse

from . import __version__
from .config import Config
from .db import ARTIFACT_KINDS, WORKER_SYNC_MODES, Database, new_id, now_iso, resolve_in_store
from .jobs import JobManager, TERMINAL_STATES, verify_callback_token
from .outbound import OutboundService, OutboundSettings
from .packs import export_pack, import_pack, list_available_packs
from .router import Router
from .thclaws_client import ThClawsClient, ThClawsError, redact_tool_payload_for_read
from .usage import audit_csv, normalize_usage_range, summarize_usage, usage_csv
from .workflow_templates import workflow_templates
from .workflows import (
    WORKFLOW_POLICY_LIMITS,
    WorkflowRunner,
    WorkflowTriggerService,
    _string_list,
    _worker_matches_role,
    next_fire_at_for_trigger,
    validate_workflow_graph,
    validate_workflow_policy,
    validate_workflow_references,
    validate_workflow_trigger_payload,
)


STATIC_DIR = Path(__file__).parent / "static"
# Safety caps live in atlas/workflows.py so the workflow API and pack import share them.
_WORKFLOW_POLICY_LIMITS = WORKFLOW_POLICY_LIMITS
_WORKFLOW_POLICY_DEFAULTS = {
    "max_jobs": 20,
    "max_iterations": 5,
    "max_attempts_per_node": 3,
    "max_minutes": 30,
    "stop_on_first_failure": True,
}
# Cap applied to a worker-callback body BEFORE any byte is read (the route is pre-auth, so an
# unauthenticated peer must never make Atlas buffer an unbounded body). Covers the terminal
# payload's summary text with ample headroom.
_CALLBACK_MAX_BODY_BYTES = 4 * 1024 * 1024
# Minimum spacing between durable job.callback_rejected audit rows for the SAME job.
_CALLBACK_REJECT_AUDIT_WINDOW_SECONDS = 60.0
# Reading the callback body is bounded in TIME as well as size: a token-holding worker that
# declares a Content-Length and then drips/withholds bytes must not pin handler threads
# indefinitely (per-recv socket timeout catches total silence; the wall-clock deadline
# catches a slow drip that resets the per-recv timer).
_CALLBACK_BODY_RECV_TIMEOUT_SECONDS = 10.0
_CALLBACK_BODY_READ_DEADLINE_SECONDS = 60.0
# Concurrent callback-body reads are slot-bounded: one valid, reusable token must not let a
# worker open unbounded parallel connections that each pin a handler thread for the whole
# read deadline. Legit deliveries beyond the bound get 503, which thClaws RETRIES (5xx).
_CALLBACK_READ_SLOTS = 8
ROLE_PERMISSIONS = {
    "admin": frozenset({"read", "audit.read", "jobs.run", "workflows.run", "approvals.decide", "workflows.manage", "workers.poll", "resources.manage", "admin", "deliveries.read"}),
    "operator": frozenset({"read", "jobs.run", "workflows.run", "approvals.decide", "workflows.manage", "workers.poll", "resources.manage", "deliveries.read"}),
    "viewer": frozenset({"read"}),
    "auditor": frozenset({"read", "audit.read", "deliveries.read"}),
}
_LIMIT_CAP = 10000


def _parse_limit(query: dict[str, list[str]], default: int = 100) -> int:
    """Clamp the ?limit query param to [1, _LIMIT_CAP]. OpenAPI declares minimum 1; a raw
    negative/zero value reaches SQLite's LIMIT and disables the bound (limit=-1 returns the
    whole table), so clamp instead of passing it through. A non-integer falls back to default."""
    try:
        value = int(query.get("limit", [str(default)])[0])
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, _LIMIT_CAP))


class AtlasRuntime:
    def __init__(self, config: Config):
        self.config = config
        self.db = Database(config.db_path, secret_key=config.secret_key)
        self.upload_dir = (config.upload_dir or config.db_path.parent / "uploads").resolve()
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.jobs = JobManager(
            self.db,
            config.request_timeout_seconds,
            public_base_url=config.public_base_url,
            secret_key=config.secret_key,
            callback_timeout_seconds=config.callback_timeout_seconds,
            upload_dir=self.upload_dir,
        )
        self.router = Router(self.db)
        self.outbound = OutboundService(
            self.db,
            OutboundSettings(
                allowlist=config.outbound_allowlist,
                secret_key=config.secret_key,
                max_attempts=config.outbound_max_attempts,
                timeout_seconds=config.outbound_timeout_seconds,
            ),
        )
        self.workflows = WorkflowRunner(
            self.db, self.jobs, outbound_allowlist=config.outbound_allowlist, outbound_service=self.outbound
        )
        self.triggers = WorkflowTriggerService(self.db, self.workflows)
        # Reconcile jobs first so orphaned worker jobs are terminal before runs recover,
        # then reconcile workflow runs (which re-arm interrupted nodes).
        self.jobs.reconcile_jobs()
        self.workflows.reconcile_runs()
        # T3: callback-pending jobs survive reconcile (they run remotely); the reaper owns
        # their deadline, so it must restart with the runtime.
        self.jobs.start_callback_reaper()
        # Rate limiter for rejected-callback audit rows (see _handle_worker_callback): at most
        # one durable row per job per window, so even a peer holding a REAL job id cannot grow
        # the DB/WAL without bound. In-memory is fine — this bounds durable writes, and a
        # restart resetting the window only permits one extra row. The lock makes the
        # check-and-reserve atomic: concurrent rejections for one job must not all observe the
        # stale timestamp and each write a row.
        self.callback_reject_audited_at: dict[str, float] = {}
        self.callback_reject_audit_lock = threading.Lock()
        self.callback_read_slots = threading.BoundedSemaphore(_CALLBACK_READ_SLOTS)
        # OB-1 restart recovery: no delivery-attempt thread survives a restart either. Off the
        # main thread since a large backlog of stuck/missing deliveries could otherwise block
        # server startup for as long as their retry loops take.
        threading.Thread(target=self.outbound.reconcile, name="atlas-outbound-reconcile", daemon=True).start()


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

    def handle_one_request(self) -> None:
        self._t0 = time.monotonic()
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            # Client dropped the connection (browser speculative sockets,
            # closed tabs); harmless — suppress the stdlib traceback spam.
            self.close_connection = True

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        # Structured (JSON) request log behind ATLAS_REQUEST_LOG; off by default so
        # response shapes and stdout stay untouched. ponytail: one line to stderr.
        server = getattr(self, "server", None)
        runtime = getattr(server, "runtime", None)
        if runtime is None or not runtime.config.request_log:
            return
        try:
            status: Any = int(code)
        except (TypeError, ValueError):
            status = code
        t0 = getattr(self, "_t0", None)
        raw_path = getattr(self, "path", None)
        # Path only — never the query string, which can carry ?token=<api-token>
        # (SSE/EventSource auth). Keeps tokens out of stderr/journald.
        record = {
            "ts": now_iso(),
            "method": getattr(self, "command", None),
            "path": urlparse(raw_path).path if raw_path else None,
            "status": status,
            "client": self.client_address[0] if self.client_address else None,
            "dur_ms": None if t0 is None else round((time.monotonic() - t0) * 1000, 1),
        }
        sys.stderr.write(json.dumps(record, ensure_ascii=True) + "\n")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            # Unauthenticated liveness probe (compose/systemd/Fleet health). Additive,
            # not under /api/, and intentionally leaks nothing (no db path / counts).
            if method == "GET" and path == "/healthz":
                self._json({"ok": True, "service": "atlas-control-plane", "version": __version__})
                return
            if path.startswith("/api/"):
                parts = [part for part in path.split("/") if part]
                if method == "POST" and parts == ["api", "auth", "login"]:
                    self._handle_api(method, path, parse_qs(parsed.query))
                    return
                if method == "POST" and len(parts) == 3 and parts[:2] == ["api", "worker-callbacks"]:
                    # Deliberate, documented pre-auth carve-out (docs/specs/threat-model.md):
                    # a thClaws terminal callback carries the per-dispatch HMAC api_key, not a
                    # user token, so routing it through _is_authorized() would 401 it before
                    # its handler. The dedicated handler enforces its own boundary: body-size
                    # cap before reading, constant-time HMAC verification bound to this job id,
                    # idempotent apply, and the `system:worker-callback` audit actor.
                    self._handle_worker_callback(parts[2])
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

        if method == "GET" and parts == ["api", "metrics"]:
            # Aggregate counters only (states, totals, schema_version) — nothing a
            # `read`-role caller could not already list item-by-item, so plain read RBAC.
            self._json({"metrics": {**runtime.db.metrics_snapshot(), "version": __version__, "time": now_iso()}})
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

        if len(parts) == 4 and parts[:2] == ["api", "workers"] and parts[3] == "sync-mode" and method == "POST":
            worker_id = parts[2]
            worker = runtime.db.get_worker(worker_id)
            if not worker:
                raise FileNotFoundError()
            mode = str((self._read_json() or {}).get("sync_mode") or "").strip()
            if mode not in WORKER_SYNC_MODES:
                raise ValueError(f"sync_mode must be one of {sorted(WORKER_SYNC_MODES)}")
            # Enabling an approved shape MUST validate reachability through the SAME worker client
            # path before persisting: an operator asserting 'tunnel'/'forward_auth' over a dead or
            # misconfigured path is rejected here, not discovered later at collection time. This
            # authenticated pre-enable transition is the ONLY caller that probes while the mode is
            # still 'disabled' (the normal poll never probes a disabled worker). The probe proves
            # reachability + response shape, NOT that the path is private — that stays the
            # operator's asserted trust. A probe failure leaves the persisted mode unchanged.
            if mode != "disabled":
                client = ThClawsClient(worker["base_url"], worker.get("token"), timeout=runtime.jobs.request_timeout_seconds)
                try:
                    client.sync_stat()
                except ThClawsError as exc:
                    raise ValueError(f"sync probe failed; sync_mode unchanged: {exc}") from exc
            updated = runtime.db.set_worker_sync_mode(worker_id, mode)
            if not updated:
                raise FileNotFoundError()
            self._json({"worker": _public_worker(updated)})
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
                limit = _parse_limit(query)
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

        if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "artifacts" and method == "GET":
            # T5: a standalone job's collected files are file_ref artifacts keyed to the job
            # (run_id is NULL when it's not a workflow node), so the run-scoped artifacts route
            # can't surface them. Mirror it per job so the dashboard can list + download them.
            if not runtime.db.get_job(parts[2]):
                raise FileNotFoundError()
            # limit=1000 comfortably clears the collect_files cap (ATLAS_SYNC_MAX_FILES, default
            # 200) — the default 100 would silently drop the oldest files past 100 with no signal.
            self._json({"artifacts": [_public_artifact(artifact) for artifact in runtime.db.list_artifacts(job_id=parts[2], limit=1000)]})
            return

        if parts == ["api", "workflows"]:
            if method == "GET":
                limit = _parse_limit(query)
                self._json({"workflows": runtime.db.list_workflow_definitions(limit)})
                return
            if method == "POST":
                payload = self._read_json()
                # name is OPTIONAL here per OpenAPI WorkflowCreateInput (required: [graph];
                # name defaults to "Untitled workflow"). Do NOT require it — that would break
                # the published additive contract. The AI-draft path still requires name via
                # _validate_workflow_draft, matching the stricter ai-draft schema.
                _validate_workflow_payload(runtime, payload)
                workflow = runtime.db.create_workflow_definition(payload)
                self._json({"workflow": workflow}, HTTPStatus.CREATED)
                return

        if parts == ["api", "workflow-templates"] and method == "GET":
            self._json({"templates": workflow_templates()})
            return

        if parts == ["api", "packs"] and method == "GET":
            self._json({"packs": list_available_packs()})
            return

        if parts == ["api", "packs", "import"] and method == "POST":
            result = import_pack(
                runtime.db,
                self._read_json(),
                secret_key=runtime.config.secret_key,
                require_signature=runtime.config.require_signed_packs,
            )
            self._json(result, HTTPStatus.CREATED)
            return

        if len(parts) == 4 and parts[:2] == ["api", "packs"] and parts[3] == "export" and method == "GET":
            self._json({"pack": export_pack(runtime.db, parts[2])})
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
                _validate_workflow_metadata(payload)
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
                limit = _parse_limit(query)
                workflow_definition_id = query.get("workflow_definition_id", [""])[0] or None
                self._json({"runs": runtime.db.list_workflow_runs(limit, workflow_definition_id)})
                return
            if method == "POST":
                payload = self._read_json()
                workflow_definition_id = payload.get("workflow_definition_id")
                if not workflow_definition_id:
                    raise ValueError("workflow_definition_id is required")
                run_input = payload.get("input")
                if run_input is None:
                    run_input = {}
                if not isinstance(run_input, dict):
                    # OpenAPI types input as an object; reject up front (400) instead of
                    # creating a run that fails asynchronously once the engine touches it.
                    # Normalize only missing/None — a falsy non-object ([], "", 0) is rejected.
                    raise ValueError("input must be an object")
                run = runtime.workflows.start_workflow(workflow_definition_id, run_input)
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
            # Same rationale as the per-job route: default 100 would silently truncate a run's
            # artifacts (a workflow node can collect up to ATLAS_SYNC_MAX_FILES too).
            self._json({"artifacts": [_public_artifact(artifact) for artifact in runtime.db.list_artifacts(run_id=parts[2], limit=1000)]})
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
            limit = _parse_limit(query, 500)
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
            if action == "deliver":
                run = runtime.db.get_workflow_run(parts[2])
                if not run:
                    raise FileNotFoundError()
                self._json({"delivery": runtime.outbound.deliver_run(run)}, HTTPStatus.ACCEPTED)
                return

        if parts == ["api", "approvals"] and method == "GET":
            limit = _parse_limit(query)
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
                limit = _parse_limit(query)
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
            body = self._read_json()
            # Pass the payload through unchanged (missing/null -> None); fire_trigger normalizes
            # None to {} and rejects any non-object value, so [] / "" / 0 don't slip in as {}.
            result = runtime.triggers.fire_trigger(parts[2], body.get("payload"), body.get("dedupe_key"))
            self._json(result, HTTPStatus.ACCEPTED)
            return

        if len(parts) == 4 and parts[:2] == ["api", "workflow-triggers"] and parts[3] == "events" and method == "GET":
            if not runtime.db.get_workflow_trigger(parts[2]):
                raise FileNotFoundError()
            limit = _parse_limit(query)
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
            limit = _parse_limit(query)
            from_at, to_at = normalize_usage_range(
                query.get("from", [""])[0] or None,
                query.get("to", [""])[0] or None,
            )
            entries = runtime.db.list_audit(limit, from_at, to_at)
            output_format = query.get("format", ["json"])[0].lower()
            if output_format == "json":
                self._json({"audit": entries})
                return
            if output_format == "csv":
                self._csv(audit_csv(entries))
                return
            raise ValueError("audit format must be json or csv")

        if parts == ["api", "deliveries"] and method == "GET":
            limit = _parse_limit(query)
            run_id = query.get("run_id", [""])[0] or None
            status = query.get("status", [""])[0] or None
            self._json({"deliveries": runtime.db.list_deliveries(limit, run_id, status)})
            return

        if len(parts) == 4 and parts[:2] == ["api", "deliveries"] and parts[3] == "retry" and method == "POST":
            if not runtime.db.get_delivery(parts[2]):
                raise FileNotFoundError()
            self._json({"delivery": runtime.outbound.retry_delivery(parts[2])}, HTTPStatus.ACCEPTED)
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
        target = resolve_in_store(runtime.upload_dir, artifact.get("content"))
        if target is None:
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
                # Redact on read: legacy rows written before T2's write-time projection can still
                # hold raw tool/skill input/output; project them here so no raw payload ever
                # leaves the server (dashboard Events pane or any API consumer).
                payload = redact_tool_payload_for_read(row["event_type"], dict(row.get("payload") or {}))
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

    def _handle_worker_callback(self, job_id: str) -> None:
        """Dedicated pre-auth handler for POST /api/worker-callbacks/{job_id} (T3). Its own
        trust boundary, checked in this order: (1) body-size cap from Content-Length BEFORE any
        byte is read; (2) constant-time verification of the per-dispatch signed token (the
        Bearer value thClaws sends is the x_callback api_key Atlas minted — bound to this job
        id + expiry, so neither cross-job replay nor post-expiry delivery verifies); only then
        (3) body read + idempotent apply under the system audit actor. Non-2xx responses here
        are terminal for the worker (thClaws abandons delivery on any non-429 4xx), which is
        exactly right: an unverifiable delivery must not be retried into the void."""
        runtime = self.server.runtime
        # Close the connection on this whole path. Every rejection below happens BEFORE the
        # declared body is consumed, so on an HTTP/1.1 keep-alive connection (e.g. a reverse
        # proxy reusing a backend socket) the unread body bytes would otherwise be parsed as
        # the next request and desync the connection. thClaws opens a fresh connection per
        # delivery, so closing after each callback costs nothing.
        self.close_connection = True
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("Content-Length is required")
        length = int(raw_length)  # non-integer -> ValueError -> 400
        if length <= 0:
            raise ValueError("callback body is required")
        if length > _CALLBACK_MAX_BODY_BYTES:
            # Rejecting WITHOUT reading leaves the body unread on the socket; close the
            # connection so those bytes can never be parsed as a follow-up request.
            self.close_connection = True
            self._json({"error": "callback body too large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        authorization = self.headers.get("Authorization") or ""
        token = authorization[7:] if authorization.startswith("Bearer ") else ""
        secret_key = runtime.config.secret_key
        # Fail closed when no signing key is configured (submit already rejects async jobs
        # then, so nothing legitimate can arrive here).
        if not secret_key or not token or not verify_callback_token(job_id, token, secret_key):
            self.close_connection = True  # body unread here too — see the 413 branch
            # Durable audit ONLY when the job id is real (junk ids from unauthenticated
            # peers must not write anything), AND at most once per job per window — a
            # compromised worker legitimately KNOWS real job ids, so per-request rows would
            # still let it grow the DB/WAL without bound. First rejection per job stays a
            # recorded security signal; repeats within the window are dropped. The window
            # slot is check-and-reserved under a lock, so concurrent rejections cannot all
            # observe the stale timestamp and each write a row.
            if runtime.db.get_job(job_id):
                now = time.monotonic()
                with runtime.callback_reject_audit_lock:
                    last_audit = runtime.callback_reject_audited_at.get(job_id)
                    audit_allowed = last_audit is None or now - last_audit >= _CALLBACK_REJECT_AUDIT_WINDOW_SECONDS
                    if audit_allowed and len(runtime.callback_reject_audited_at) >= 1024 and job_id not in runtime.callback_reject_audited_at:
                        # Evict EXPIRED windows only — clearing everything would let a worker
                        # rotating >1024 real job ids reset every window and write without
                        # bound. If the cache is saturated with in-window entries, fail
                        # CLOSED (skip the row): durable writes stay bounded at 1024/window.
                        cutoff = now - _CALLBACK_REJECT_AUDIT_WINDOW_SECONDS
                        for stale_key in [key for key, stamped in runtime.callback_reject_audited_at.items() if stamped < cutoff]:
                            del runtime.callback_reject_audited_at[stale_key]
                        if len(runtime.callback_reject_audited_at) >= 1024:
                            audit_allowed = False
                    if audit_allowed:
                        runtime.callback_reject_audited_at[job_id] = now
                if audit_allowed:
                    runtime.db.audit(
                        "job.callback_rejected", "job", job_id,
                        {"reason": "invalid_or_expired_token"}, actor="system:worker-callback",
                    )
            self._json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return
        job = runtime.db.get_job(job_id)
        if not job:
            raise FileNotFoundError()
        if not runtime.callback_read_slots.acquire(blocking=False):
            # All slots busy: bounded thread usage beats availability here. 503 is a retryable
            # signal for thClaws (only non-429 4xx aborts delivery).
            self.close_connection = True
            self._json({"error": "callback delivery is busy; retry"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        # Hold the slot through the WHOLE of read + parse + apply, not just the read: the
        # JSON decode, token scans, event projection, and DB work are the heavy part, and a
        # token-holding worker replaying many 4 MiB bodies concurrently would otherwise blow
        # past the 8-slot bound the moment each released after its read. Slot count bounds
        # concurrent processing end-to-end.
        try:
            raw_body = self._read_callback_body(length)
            if raw_body is None:
                return  # timed out mid-read; response already sent, connection closing
            payload = json.loads(raw_body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("callback body must be a JSON object")
            with runtime.db.as_actor("system:worker-callback"):
                # Pass the verified token so a worker error message reflecting it is redacted
                # before any terminal field (jobs.error / event / audit) is persisted.
                result = runtime.jobs.apply_worker_callback(job_id, payload, token=token)
            self._json(result)
        finally:
            runtime.callback_read_slots.release()

    def _read_callback_body(self, length: int) -> bytes | None:
        """Read exactly `length` callback-body bytes with BOTH a per-recv socket timeout and a
        wall-clock deadline. A token-holding worker that drips one byte per recv-timeout window
        would otherwise pin this handler thread forever (each byte resets the socket timer);
        the deadline bounds the whole read. Returns None after answering a RETRYABLE 503 on
        timeout — pre-auth threads must always be reclaimable, and thClaws abandons delivery on
        any non-429 4xx, so a 408 would turn a transient network stall into permanent result
        loss (the job would sit until the reaper fails it). 503 keeps the worker retrying."""
        self.connection.settimeout(_CALLBACK_BODY_RECV_TIMEOUT_SECONDS)
        deadline = time.monotonic() + _CALLBACK_BODY_READ_DEADLINE_SECONDS
        chunks = bytearray()
        # read1 (one underlying recv per call), NOT read(n): BufferedReader.read(n) loops
        # internally until n bytes arrive, so a drip would keep it inside ONE call and this
        # loop's deadline check would never run (same discipline as iter_sse's chunked read).
        read = getattr(self.rfile, "read1", None) or self.rfile.read
        try:
            while len(chunks) < length:
                if time.monotonic() > deadline:
                    raise TimeoutError("callback body read exceeded its deadline")
                chunk = read(min(65536, length - len(chunks)))
                if not chunk:
                    raise ValueError("callback body is incomplete")
                chunks += chunk
        except (TimeoutError, OSError):
            self.close_connection = True
            self._json({"error": "callback body read timed out; retry"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return None
        return bytes(chunks)

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
        self.auth_identity: dict[str, Any] = {}
        if runtime.config.enable_loopback_without_token and self.client_address[0] in {"127.0.0.1", "::1"}:
            self.auth_identity = {"id": None, "username": "local", "role": "admin", "status": "active", "token_id": None}
            return True
        authorization = self.headers.get("Authorization") or ""
        bearer_token = authorization[7:] if authorization.startswith("Bearer ") else None
        raw_token = bearer_token
        if not raw_token:
            # The ?token= query fallback exists ONLY for browser EventSource, which cannot set
            # an Authorization header — and is restricted to the SSE event streams so a
            # long-lived token can't be placed in (and logged from) arbitrary request URLs.
            parsed = urlparse(self.path)
            if self.command == "GET" and parsed.path.endswith("/events"):
                raw_token = parse_qs(parsed.query).get("token", [None])[0]
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
    if parts[:2] == ["api", "packs"]:
        return "workflows.manage" if method == "POST" else "read"
    if parts[:2] == ["api", "deliveries"]:
        return "deliveries.read" if method == "GET" else "workflows.run"
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
    if payload.get("job_id"):
        if not runtime.db.get_job(payload["job_id"]):
            raise ValueError(f"Unknown job_id: {payload['job_id']}")
        # An artifact belongs to its run: a supplied job_id must be a job of THIS run. This
        # rejects both a cross-run job and a standalone job with no workflow context (which
        # would otherwise slip through and attach to an arbitrary run).
        context = runtime.db.workflow_context_for_job(payload["job_id"])
        if context.get("run_id") != run_id:
            raise ValueError("job_id does not belong to the workflow run")
    if payload.get("metadata") is not None and not isinstance(payload.get("metadata"), dict):
        raise ValueError("artifact metadata must be an object")


def _validate_workflow_payload(runtime: AtlasRuntime, payload: dict[str, Any], require_name: bool = False) -> None:
    if require_name and (not isinstance(payload.get("name"), str) or not payload["name"].strip()):
        # The schema requires name (minLength 1). Without this the server silently persists
        # a missing name as "Untitled workflow", disagreeing with any schema-conformant client.
        raise ValueError("workflow name is required")
    if "graph" not in payload:
        raise ValueError("workflow graph is required")
    graph = payload["graph"]
    policy = payload.get("policy") or {}
    validate_workflow_graph(graph, policy)
    validate_workflow_references(runtime.db, graph, policy)
    _validate_workflow_policy(policy)
    _validate_workflow_draft_triggers(payload.get("triggers") or [])


_WORKFLOW_DRAFT_FIELDS = {"name", "description", "graph", "policy", "triggers", "explanation", "warnings"}


def _validate_workflow_draft(runtime: AtlasRuntime, draft: dict[str, Any]) -> None:
    """Validate an AI-produced draft against docs/specs/workflow-ai-draft.schema.json before
    it is returned: required fields present and non-empty, no unknown fields, and
    graph/policy/triggers valid. triggers/warnings are defaulted by the caller before this
    runs (matching the importer normalization), so the full field set is always present."""
    if not isinstance(draft, dict):
        raise ValueError("workflow draft must be an object")
    missing = sorted(_WORKFLOW_DRAFT_FIELDS - set(draft))
    if missing:
        raise ValueError(f"workflow draft missing required field(s): {', '.join(missing)}")
    unknown = sorted(set(draft) - _WORKFLOW_DRAFT_FIELDS)
    if unknown:
        raise ValueError(f"workflow draft has unknown field(s): {', '.join(unknown)}")
    for field in ("name", "explanation"):
        if not isinstance(draft.get(field), str) or not draft[field].strip():
            raise ValueError(f"workflow draft {field} must be a non-empty string")
    if not isinstance(draft.get("description"), str):
        raise ValueError("workflow draft description must be a string")
    # Raw type checks against the schema BEFORE _validate_workflow_payload, whose `or {}`/`or []`
    # coercion would otherwise let null/[]/wrong-typed fields through (e.g. policy=[], triggers=null).
    if not isinstance(draft.get("graph"), dict):
        raise ValueError("workflow draft graph must be an object")
    if not isinstance(draft.get("policy"), dict):
        raise ValueError("workflow draft policy must be an object")
    if not isinstance(draft.get("triggers"), list):
        raise ValueError("workflow draft triggers must be a list")
    if not isinstance(draft.get("warnings"), list) or not all(isinstance(item, str) for item in draft["warnings"]):
        raise ValueError("workflow draft warnings must be a list of strings")
    _validate_workflow_payload(runtime, draft, require_name=True)


def _validate_workflow_metadata(payload: dict[str, Any]) -> None:
    """Validate the additive metadata fields a PUT can persist. OpenAPI types `version` as
    an integer; reject a non-integer (a string version is stored as-is and later breaks pack
    export). `status` feeds a dashboard class attribute, so restrict it to a safe token
    (defense in depth with the client-side sanitizer). Normalizes a digit-string version."""
    if "version" in payload:
        version = payload["version"]
        if isinstance(version, str) and version.isdigit():
            version = int(version)
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("workflow version must be an integer >= 1")
        payload["version"] = version
    if "status" in payload:
        status = payload["status"]
        if not isinstance(status, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", status):
            raise ValueError("workflow status must match [A-Za-z0-9_-]+")


def _validate_workflow_policy(policy: dict[str, Any]) -> None:
    validate_workflow_policy(policy)


def _validate_workflow_draft_triggers(triggers: Any) -> None:
    if not isinstance(triggers, list):
        raise ValueError("workflow draft triggers must be a list")
    for index, trigger in enumerate(triggers):
        if not isinstance(trigger, dict):
            raise ValueError(f"workflow draft trigger at index {index} must be an object")
        validate_workflow_trigger_payload(trigger)


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
    draft.setdefault("triggers", [])
    draft.setdefault("warnings", [])
    _validate_workflow_draft(runtime, draft)
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
        draft.setdefault("triggers", [])
        draft.setdefault("warnings", [])
        _validate_workflow_draft(runtime, draft)
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
    return cast("list[dict[str, Any]]", triggers)


def _suggest_workflow_workers(runtime: AtlasRuntime, payload: dict[str, Any]) -> list[dict[str, Any]]:
    graph = payload.get("graph")
    if not isinstance(graph, dict):
        raise ValueError("workflow graph must be an object")
    policy = payload.get("policy") or {}
    validate_workflow_graph(graph, policy)
    _validate_workflow_policy(policy)
    validate_workflow_references(runtime.db, graph, policy, allow_unresolved_roles=True)
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


def cli_config(argv: list[str] | None = None) -> Config:
    """Build the runtime Config from env + CLI overrides. Kept separate from main() so the
    override path is testable without starting the server. The reconstruction copies EVERY
    Config field from the env-derived base — dropping one (e.g. require_signed_packs) would
    silently disable it for the canonical run.sh / run-prod.sh launch paths."""
    parser = argparse.ArgumentParser(description="Atlas control plane")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--db", default=None)
    args = parser.parse_args(argv)

    config = Config.from_env()
    if args.host or args.port or args.db:
        db_path = Path(args.db).resolve() if args.db else config.db_path
        upload_dir = db_path.parent / "uploads" if args.db and "ATLAS_UPLOAD_DIR" not in os.environ else config.upload_dir
        # Copy from the base so new Config fields survive overrides by default.
        config = replace(config, host=args.host or config.host, port=args.port or config.port, db_path=db_path, upload_dir=upload_dir)
    return config


def main(argv: list[str] | None = None) -> None:
    run_server(cli_config(argv))
