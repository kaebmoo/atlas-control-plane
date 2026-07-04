from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from .db import Database, now_iso
from .router import Router
from .thclaws_client import (
    SseEvent,
    ThClawsClient,
    ThClawsError,
    extract_session_id,
    extract_text,
    extract_usage,
    parse_event_payload,
    project_structured_event,
)
from .usage import elapsed_seconds


TERMINAL_STATES = {"succeeded", "failed", "cancelled"}
JOB_EXECUTION_MODES = {"stream", "callback"}
# A callback token must outlive the reaper deadline by the worker's whole delivery envelope:
# thClaws retries at ~0/10/60s with a 30s per-attempt request timeout (~160s worst case), plus
# margin for wall-clock skew between Atlas and the worker host (the token crosses machines, so
# it is checked against wall time, not a monotonic clock). A token that expired AT the deadline
# would 401 the worker's legitimate final retries — thClaws gives up on any non-429 4xx.
CALLBACK_RETRY_ENVELOPE_SECONDS = 300
_CALLBACK_TOKEN_DOMAIN = "atlas-worker-callback-v1"


def mint_callback_token(job_id: str, expires_epoch: int, secret_key: str) -> str:
    """Per-dispatch signed token carried as the x_callback api_key — same HMAC-SHA256 primitive
    as the signed usage export. The signature binds job_id + expiry, so a token minted for one
    job can never authorize a callback for another, and tampering with the embedded expiry
    breaks the signature. Stateless: never stored (tokens must not reach logs or SQLite)."""
    message = f"{_CALLBACK_TOKEN_DOMAIN}:{job_id}:{expires_epoch}"
    signature = hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{expires_epoch}.{signature}"


def verify_callback_token(job_id: str, token: str, secret_key: str, now_epoch: float | None = None) -> bool:
    """Constant-time verification of a mint_callback_token value against the job in the URL
    path. now_epoch is injectable so checks can probe the validity window deterministically."""
    expires_text, _, signature = token.partition(".")
    try:
        expires_epoch = int(expires_text)
    except ValueError:
        return False
    message = f"{_CALLBACK_TOKEN_DOMAIN}:{job_id}:{expires_epoch}"
    expected = hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return False
    return (time.time() if now_epoch is None else now_epoch) <= expires_epoch


def _handoff_child_id(source_job_id: str) -> str:
    """Deterministic id for a source job's handoff child. Stable across retries/restarts, so
    re-attempting a handoff never creates a second child (idempotent recovery). Distinct from
    the random new_id space; the `job_` prefix keeps it a valid job id."""
    return "job_handoff_" + hashlib.sha256(source_job_id.encode("utf-8")).hexdigest()[:24]


_CALLBACK_TOKEN_PLACEHOLDER = "[redacted-callback-token]"


def _redact_token(text: str, token: str | None) -> str:
    """Strip a callback token from any worker-controlled string before it is persisted. The
    token is a LIVE credential (valid until its expiry) that authorizes the terminal callback,
    so a semi-trusted worker echoing it into ANY stored field — session_id, summary, tool
    names, error message — would let a read-authorized user retrieve it and forge the callback.
    Redact at every persistence boundary, not just one field."""
    if not token or token not in text:
        return text
    return text.replace(token, _CALLBACK_TOKEN_PLACEHOLDER)


def _project_callback_result(payload: dict[str, Any], token: str | None = None) -> dict[str, Any]:
    """Structural projection of a worker's terminal callback payload for the job_events
    timeline. Same rule as T2's tool/skill projection: names, counters, and enum-like strings
    only — worker-controlled values are length-capped, and nothing payload-shaped is stored
    (the v1 callback shape carries tool NAMES only, no input/output). Every worker-controlled
    string is token-redacted (a name echoing the callback credential must not be persisted)."""

    def _names(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [_redact_token(item[:200], token) for item in value if isinstance(item, str)][:100]

    projected: dict[str, Any] = {
        "status": _redact_token(str(payload.get("status") or "")[:32], token),
        "finish_reason": _redact_token(str(payload.get("finish_reason") or "")[:32], token),
        "tool_calls": _names(payload.get("tool_calls")),
        "tool_denials": _names(payload.get("tool_denials")),
    }
    iterations = payload.get("iterations")
    if isinstance(iterations, int) and not isinstance(iterations, bool) and 0 <= iterations <= 2**31:
        projected["iterations"] = iterations
    return projected
DEFAULT_HANDOFF_PROMPT = """คุณได้รับผลลัพธ์จาก agent ก่อนหน้า

งานของคุณคือเรียบเรียงผลลัพธ์นี้ให้พร้อมส่งต่อผู้ใช้ โดยรักษาข้อเท็จจริง ไม่แต่งเติมข้อมูลที่ไม่มีในต้นฉบับ

ผลลัพธ์จาก agent ก่อนหน้า:
{result}
"""
# ponytail: headless jobs have no one to answer AskUserQuestion (it only resolves over
# thClaws's /ws IPC channel, not /agent/run), so a question there hangs the job for the
# full ASK_TIMEOUT. Steer the model away from it via the `system` field instead of patching
# thClaws. Revisit if Atlas ever grows a real answer-back channel for running jobs.
NO_ASK_SYSTEM_PROMPT = (
    "ห้ามใช้ AskUserQuestion หรือถามคำถามกลับผู้ใช้เด็ดขาด งานนี้รันแบบไม่มีคนคอยตอบ "
    "ถ้าต้องการข้อมูลล่าสุด ให้ใช้ WebSearch หรือ WebFetch ค้นหาเอง "
    "ถ้ายังหาคำตอบที่แน่ชัดไม่ได้ ให้ตอบเท่าที่หาได้พร้อมระบุว่าข้อมูลอาจไม่ครบ"
)
LOGGER = logging.getLogger(__name__)


class JobManager:
    def __init__(
        self,
        db: Database,
        request_timeout_seconds: float = 30,
        public_base_url: str | None = None,
        secret_key: str | None = None,
        callback_timeout_seconds: float | None = None,
    ):
        self.db = db
        self.router = Router(db)
        self.request_timeout_seconds = request_timeout_seconds
        # Backstops for a slow-dribbling / runaway worker on the (deadline-less) standalone-job
        # path: an overall wall-clock bound and a total-output cap. Generous defaults; override
        # via env. Workflow jobs additionally get the policy max_minutes deadline.
        self.max_stream_seconds = float(os.getenv("ATLAS_MAX_STREAM_SECONDS", "3600"))
        self.max_output_bytes = int(os.getenv("ATLAS_MAX_JOB_OUTPUT_BYTES", str(16 * 1024 * 1024)))
        # T3 async execution: both are preconditions for execution:"callback" (the worker must
        # be able to reach Atlas, and the callback token needs a signing key); validated at
        # submit time so a misconfigured deployment rejects async jobs with a clear 400.
        self.public_base_url = (public_base_url or "").rstrip("/") or None
        self.secret_key = secret_key
        # Explicit param wins (Config carries the env-derived value on the server path); the
        # env fallback keeps direct JobManager construction in checks/scripts working.
        self.callback_timeout_seconds = (
            float(os.getenv("ATLAS_CALLBACK_TIMEOUT_SECONDS", "3600"))
            if callback_timeout_seconds is None
            else callback_timeout_seconds
        )
        # The reaper must NOT fail a job the instant its deadline passes: the callback token is
        # deliberately kept valid for CALLBACK_RETRY_ENVELOPE_SECONDS beyond the deadline to
        # cover the worker's retry envelope, so a first delivery near the deadline that gets a
        # retryable 503/timeout still has a valid retry. Reaping before that window closes would
        # terminal-ize the job and turn the worker's authenticated retry into a lost result.
        # The reaper therefore fires at deadline + this grace, matching the token's lifetime.
        self.callback_reap_grace_seconds = float(CALLBACK_RETRY_ENVELOPE_SECONDS)
        self.trigger_service: Any = None
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()

    def submit(self, payload: dict[str, Any], *, explicit_id: str | None = None) -> dict[str, Any]:
        # explicit_id is a PRIVATE parameter (keyword-only, never read from `payload`): it lets
        # the handoff path pass a deterministic child id for idempotent recovery. It must NOT
        # come from the request body — POST /api/jobs passes the body straight here, so honoring
        # a body `id` would let a caller pre-create a job at another job's handoff id and hijack
        # the handoff linkage.
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("prompt is required")

        # Async execution is opt-in per job; default behavior is byte-identical. Validated up
        # front (like routing below) so a rejected async job never creates an orphan
        # conversation. Both preconditions carry a clear, actionable message.
        # Default ONLY on true absence — an explicit invalid value (null, "", 0, [], {}) is
        # rejected, not coerced to the default. isinstance BEFORE membership: an unhashable
        # JSON value would raise TypeError out of the set probe and surface as a 500, not 400.
        execution = payload["execution"] if "execution" in payload else "stream"
        if not isinstance(execution, str) or execution not in JOB_EXECUTION_MODES:
            raise ValueError(f"execution must be one of {sorted(JOB_EXECUTION_MODES)}, got: {execution!r}")
        if execution == "callback":
            if not self.public_base_url:
                raise ValueError("execution 'callback' requires ATLAS_PUBLIC_BASE_URL (the worker must be able to reach Atlas)")
            if not self.secret_key:
                raise ValueError("execution 'callback' requires ATLAS_SECRET_KEY (signs the callback token)")

        conversation_id = payload.get("conversation_id")
        # Resolve routing and handoff BEFORE creating a conversation, so a failure here
        # (e.g. no workers registered, unknown handoff target) doesn't leave an orphan
        # conversation behind. A brand-new conversation has no session binding, so the
        # router doesn't need its id to resolve.
        route_payload = dict(payload)
        route_payload["prompt"] = prompt
        if conversation_id:
            route_payload["conversation_id"] = conversation_id
        decision = self.router.resolve(route_payload)
        handoff = self._resolve_handoff(payload)

        if not conversation_id:
            conversation = self.db.create_conversation(
                {
                    "title": prompt[:80],
                    "workspace_key": payload.get("workspace_key") or "",
                    "company": payload.get("company") or "",
                }
            )
            conversation_id = conversation["id"]

        job = self.db.create_job(
            {
                # From the PRIVATE explicit_id kwarg only (never the request body) — handoff
                # recovery derives a DETERMINISTIC child id so a retry can't create a second.
                "id": explicit_id,
                "conversation_id": conversation_id,
                "worker_id": decision.worker["id"],
                "workspace_id": decision.workspace["id"] if decision.workspace else None,
                "parent_job_id": payload.get("parent_job_id"),
                "prompt": prompt,
                "model": payload.get("model") or "",
                "route_reason": decision.reason,
                "thclaws_session_id": decision.thclaws_session_id,
                "handoff_worker_id": handoff.get("worker_id"),
                "handoff_workspace_id": handoff.get("workspace_id"),
                "handoff_prompt": handoff.get("prompt") or "",
                "execution": execution,
            }
        )
        self.db.append_job_event(
            job["id"],
            "route",
            {
                "worker_id": decision.worker["id"],
                "worker_name": decision.worker.get("name"),
                "workspace_id": decision.workspace["id"] if decision.workspace else None,
                "workspace_key": decision.workspace.get("workspace_key") if decision.workspace else None,
                "reason": decision.reason,
            },
        )
        if handoff:
            self.db.append_job_event(
                job["id"],
                "handoff_configured",
                {
                    "worker_id": handoff.get("worker_id"),
                    "workspace_id": handoff.get("workspace_id"),
                },
            )
        self._start_thread(job["id"])
        return self.db.get_job(job["id"]) or job

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.db.get_job(job_id)
        if not job:
            raise ValueError(f"Unknown job_id: {job_id}")
        if not self.db.mark_cancel_requested(job_id):
            # The job completed between our read and the cancel write — respect its terminal
            # state instead of regressing it to cancel_requested.
            return self.db.get_job(job_id) or job
        self.db.append_job_event(job_id, "cancel_requested", {"message": "Best-effort cancel requested"})
        return self.db.get_job(job_id) or job

    def poll_worker(self, worker_id: str) -> dict[str, Any]:
        worker = self.db.get_worker(worker_id)
        if not worker:
            raise ValueError(f"Unknown worker_id: {worker_id}")
        client = ThClawsClient(worker["base_url"], worker.get("token"), timeout=self.request_timeout_seconds)
        try:
            health = client.health()
            agent_info = client.agent_info()
            # health() always returns a dict (truthy), so key off the worker's own ok flag:
            # a reachable-but-unhealthy worker ({"ok": false}) must not be ranked as online.
            status = "online" if health.get("ok") else "offline"
            merged_info = {"health": health, "agent": agent_info}
            self.db.update_worker_status(worker_id, status, merged_info, None)
        except ThClawsError as exc:
            self.db.update_worker_status(worker_id, "offline", {}, str(exc))
        updated = self.db.get_worker(worker_id) or worker
        if self.trigger_service and worker.get("status") != updated.get("status"):
            self.trigger_service.fire_internal(
                "worker_status_changed",
                {
                    "worker_id": worker_id,
                    "previous_status": worker.get("status"),
                    "status": updated.get("status"),
                    "updated_at": updated.get("updated_at"),
                },
                f"worker_status_changed:{worker_id}:{worker.get('status')}:{updated.get('status')}:{updated.get('updated_at')}",
            )
        return updated

    def poll_all_workers(self) -> list[dict[str, Any]]:
        results = []
        for worker in self.db.list_workers():
            results.append(self.poll_worker(worker["id"]))
        return results

    def reconcile_jobs(self) -> None:
        """After an Atlas restart no job threads survive, so any job still queued/running in
        the DB is orphaned — its thread is gone but the row says it is in flight. Fail those
        jobs so callers see a terminal state and usage is recorded, instead of a job wedged
        'running' forever. Idempotent: only touches non-terminal jobs with no live thread."""
        for job in self.db.list_non_terminal_jobs():
            if (job.get("execution") or "stream") == "callback" and job.get("callback_deadline_at"):
                # Callback-pending jobs are legitimately in flight on a REMOTE worker — the
                # dispatch thread exiting is their normal shape, not an interruption. Leave
                # them for the late callback or the reaper's deadline. A callback job whose
                # deadline was never written crashed BEFORE dispatch, so it falls through to
                # interrupted-job handling below like any other orphan.
                continue
            with self._lock:
                active = self._threads.get(job["id"])
            if active and active.is_alive():
                continue
            if job.get("cancel_requested"):
                # The user cancelled it before its thread observed the request; honor that as a
                # terminal 'cancelled', not 'failed' (which would mislabel the outcome + usage).
                self.db.update_job(job["id"], state="cancelled", finished_at=now_iso())
                self.db.append_job_event(job["id"], "state", {"state": "cancelled", "reason": "atlas_restarted"})
                self.db.audit("job.cancelled", "job", job["id"])
            else:
                error = "Atlas restarted while the job was in flight"
                self.db.update_job(job["id"], state="failed", error=error, finished_at=now_iso())
                self.db.append_job_event(job["id"], "state", {"state": "failed", "reason": "atlas_restarted"})
                self.db.audit("job.failed", "job", job["id"], {"error": error})
            self._record_job_usage(job["id"])

    def _start_thread(self, job_id: str) -> None:
        context = contextvars.copy_context()
        thread = threading.Thread(target=context.run, args=(self._run, job_id), name=f"atlas-job-{job_id}", daemon=True)
        with self._lock:
            self._threads[job_id] = thread
        thread.start()

    def _resolve_handoff(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw = payload.get("handoff")
        if not isinstance(raw, dict) or not raw.get("enabled"):
            return {}

        workspace_id = raw.get("workspace_id") or None
        worker_id = raw.get("worker_id") or None
        if workspace_id:
            workspace = self.db.get_workspace(workspace_id)
            if not workspace:
                raise ValueError(f"Unknown handoff workspace_id: {workspace_id}")
            worker_id = workspace["worker_id"]
        elif worker_id and not self.db.get_worker(worker_id):
            raise ValueError(f"Unknown handoff worker_id: {worker_id}")

        if not worker_id:
            raise ValueError("handoff requires worker_id or workspace_id")

        return {
            "worker_id": worker_id,
            "workspace_id": workspace_id,
            "prompt": str(raw.get("prompt") or DEFAULT_HANDOFF_PROMPT),
        }

    def _run(self, job_id: str) -> None:
        job = self.db.get_job(job_id)
        if not job:
            return
        worker = self.db.get_worker(job["worker_id"])
        workspace = self.db.get_workspace(job["workspace_id"]) if job.get("workspace_id") else None
        if not worker:
            self.db.update_job(job_id, state="failed", error="Worker disappeared", finished_at=now_iso())
            self.db.append_job_event(job_id, "error", {"error": "Worker disappeared"})
            self._record_job_usage(job_id)
            with self._lock:
                self._threads.pop(job_id, None)
            return

        # Atomically claim queued -> running, but only if no cancel has been requested. This
        # closes the TOCTOU window where a cancel landing between a plain check and the state
        # write would still open the worker stream (a remote side effect on a cancelled job).
        if not self.db.try_start_job(job_id):
            if self.db.is_cancel_requested(job_id):
                self.db.update_job(job_id, state="cancelled", finished_at=now_iso())
                self.db.append_job_event(job_id, "state", {"state": "cancelled"})
                self.db.audit("job.cancelled", "job", job_id)
                self._record_job_usage(job_id)
            with self._lock:
                self._threads.pop(job_id, None)
            return
        self.db.append_job_event(job_id, "state", {"state": "running"})
        client = ThClawsClient(worker["base_url"], worker.get("token"), timeout=self.request_timeout_seconds)
        if (job.get("execution") or "stream") == "callback":
            # Fire-and-forget: handled OUTSIDE the stream path's try/finally on purpose. The
            # shared finally records usage whenever the job is terminal — but a fast callback
            # can terminal-ize the job while this thread is still unwinding, and its NULL-token
            # usage row would then win the idempotent usage INSERT over the callback's real
            # counts. The callback/reaper owns a dispatched job's usage; this thread records
            # usage only when IT terminal-izes the job (dispatch failure / early cancel).
            self._run_callback_dispatch(job_id, job, worker, workspace, client)
            return
        done_seen = False
        stream_deadline = time.monotonic() + self.max_stream_seconds
        usage: dict[str, int] | None = None
        try:
            # max_total_bytes bounds the CUMULATIVE raw worker output in iter_sse, at the byte
            # source — so every wire byte counts (data, framing/whitespace padding, comment and
            # data-less frames), and no frame shape can push traffic past the configured cap.
            for event in client.run_agent_stream(
                prompt=job["prompt"],
                workspace_dir=workspace.get("workspace_dir") if workspace else None,
                system=NO_ASK_SYSTEM_PROMPT,
                model=job.get("model") or None,
                session_id=job.get("thclaws_session_id") or None,
                stream_deadline=stream_deadline,
                max_total_bytes=self.max_output_bytes,
            ):
                if self.db.is_cancel_requested(job_id):
                    raise _JobCancelled()
                if time.monotonic() > stream_deadline:
                    # Overall wall-clock bound: a worker dribbling bytes can't pin this thread
                    # forever (the per-recv socket timeout doesn't bound total stream duration).
                    raise ThClawsError(f"worker stream exceeded {self.max_stream_seconds:.0f}s without completing")

                payload = parse_event_payload(event)
                if event.data == "[DONE]":
                    self.db.append_job_event(job_id, "done", payload)
                    done_seen = True
                    break

                parsed_usage = extract_usage(event)
                if parsed_usage is not None:
                    # Merge per key, last-seen wins: a retried turn re-emits final counts,
                    # but a partial frame must never clobber counts already seen back to NULL.
                    usage = (usage or {}) | parsed_usage

                session_id = extract_session_id(event)
                if session_id:
                    self._record_session(job, worker, workspace, session_id)
                    # Fall through (no `continue`): a single frame can carry BOTH a session id
                    # and assistant text — dropping the text here would silently lose output.

                text = extract_text(event)
                if text:
                    self.db.append_job_text(job_id, text)
                elif event.event != "session":
                    # Structured/generic frame (tool_*/skill_*/thinking/unknown), possibly ALSO
                    # carrying a session id — store it regardless (gating on `not session_id`
                    # would silently drop, e.g., a tool_use_result that also carries session_id,
                    # and lose it from the timeline). Only the dedicated `session` frame — already
                    # stored above — is skipped. Tool & skill events are projected to structural
                    # metadata BEFORE storage so raw tool input/output (possible secrets/BYOK
                    # keys) never reach SQLite; other events pass through. Bytes already counted.
                    event_type = event.event or "message"
                    self.db.append_job_event(job_id, event_type, project_structured_event(event_type, payload))

            if self.db.is_cancel_requested(job_id):
                raise _JobCancelled()
            if not done_seen:
                # Stream ended without a terminal [DONE] frame — the worker disconnected
                # mid-output. Fail rather than report success so a truncated result is never
                # handed off as complete.
                raise ThClawsError("worker stream ended without a terminal [DONE] frame")
            self.db.update_job(job_id, state="succeeded", finished_at=now_iso())
            self.db.append_job_event(job_id, "state", {"state": "succeeded"})
            self.db.audit("job.succeeded", "job", job_id)
            self._maybe_start_handoff(job_id)
        except _JobCancelled:
            self.db.update_job(job_id, state="cancelled", finished_at=now_iso())
            self.db.append_job_event(job_id, "state", {"state": "cancelled"})
            self.db.audit("job.cancelled", "job", job_id)
        except Exception as exc:
            if self.db.is_cancel_requested(job_id):
                self.db.update_job(job_id, state="cancelled", finished_at=now_iso())
                self.db.append_job_event(job_id, "state", {"state": "cancelled"})
                self.db.audit("job.cancelled", "job", job_id)
                return
            self.db.update_job(job_id, state="failed", error=str(exc), finished_at=now_iso())
            self.db.append_job_event(job_id, "error", {"error": str(exc)})
            self.db.audit("job.failed", "job", job_id, {"error": str(exc)})
        finally:
            self._record_job_usage(job_id, usage)
            with self._lock:
                self._threads.pop(job_id, None)

    def _run_callback_dispatch(
        self,
        job_id: str,
        job: dict[str, Any],
        worker: dict[str, Any],
        workspace: dict[str, Any] | None,
        client: ThClawsClient,
    ) -> None:
        """Callback-mode lifecycle of the dispatch thread. On a clean 202 ACK it writes NO
        terminal state and NO usage — the callback (or reaper) owns both. Only a DEFINITIVE
        pre-acceptance failure (the worker answered an HTTP error, or we failed before the
        POST) terminal-izes the job here; an AMBIGUOUS network failure leaves it
        callback-pending, because the worker may be running with a valid token and failing the
        job would discard its future result. Terminal writes go through the same atomic
        single-transaction apply as the callback/reaper (a racing cancel atomically wins as
        'cancelled')."""
        try:
            self._dispatch_callback(job_id, job, worker, workspace, client)
        except _JobCancelled:
            self._finish_failed_dispatch(job_id, job, "cancelled", None)
        except _CallbackDispatchUnconfirmed as exc:
            # The deadline was written before the POST, so the job stays bounded either way:
            # a real callback completes it, or the reaper fails it at the deadline.
            self.db.append_job_event(job_id, "callback_dispatch_unconfirmed", {"error": str(exc)})
        except Exception as exc:
            self._finish_failed_dispatch(job_id, job, "failed", str(exc))
        finally:
            with self._lock:
                self._threads.pop(job_id, None)

    def _finish_failed_dispatch(self, job_id: str, job: dict[str, Any], state: str, error: str | None) -> None:
        """Terminal-ize a callback job whose dispatch definitively failed (or was cancelled
        pre-POST) through the same single-transaction apply as the callback/reaper — a racing
        cancel or an already-delivered result converges the same way everywhere."""
        finished_at = now_iso()
        # Refetch: the caller's snapshot predates try_start_job, so its started_at is still
        # NULL — metering from it would record a null start/duration on the immutable row.
        job = self.db.get_job(job_id) or job
        self.db.apply_job_terminal_result(
            job_id,
            state,
            finished_at=finished_at,
            error=error,
            usage_payload=self._usage_payload(job, None, finished_at),
        )

    def _record_session(self, job: dict[str, Any], worker: dict[str, Any], workspace: dict[str, Any] | None, session_id: str) -> None:
        """Persist a worker-reported session id on the job (and the conversation's primary
        binding). Shared by the stream loop and the callback dispatch ACK so the binding rule
        stays single-sourced: a handoff child (has parent_job_id) never repoints the
        conversation's binding — the handoff worker is a transient post-processor, not the
        conversation's owner. Only the originating job writes the binding."""
        job_id = job["id"]
        self.db.update_job(job_id, thclaws_session_id=session_id)
        if job.get("conversation_id") and not job.get("parent_job_id"):
            self.db.upsert_session_binding(
                job["conversation_id"],
                worker["id"],
                workspace["id"] if workspace else None,
                session_id,
            )
        self.db.append_job_event(job_id, "session", {"session_id": session_id})

    def _dispatch_callback(
        self,
        job_id: str,
        job: dict[str, Any],
        worker: dict[str, Any],
        workspace: dict[str, Any] | None,
        client: ThClawsClient,
    ) -> None:
        """Dispatch an execution:'callback' job: POST /agent/run with the x_callback envelope
        and stop at the worker's 202 ACK. The terminal payload arrives later at
        POST /api/worker-callbacks/{job_id}, authorized by the per-dispatch signed token minted
        here (carried as the envelope's api_key; never logged or stored)."""
        if self.db.is_cancel_requested(job_id):
            raise _JobCancelled()
        if not self.public_base_url or not self.secret_key:
            # submit() rejects this up front; direct create_job callers get the same message.
            raise ThClawsError("execution 'callback' requires ATLAS_PUBLIC_BASE_URL and ATLAS_SECRET_KEY")
        deadline_at = (datetime.now(UTC) + timedelta(seconds=self.callback_timeout_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
        expires_epoch = int(time.time() + self.callback_timeout_seconds + CALLBACK_RETRY_ENVELOPE_SECONDS)
        # The deadline is written BEFORE the worker POST: a crash between the write and the
        # POST leaves a job the reaper fails at its deadline, a crash after leaves a pending
        # job the late callback (or reaper) resolves — either way the job is bounded, never
        # wedged with no owner.
        self.db.update_job(job_id, callback_deadline_at=deadline_at)
        token = mint_callback_token(job_id, expires_epoch, self.secret_key)
        try:
            ack = client.run_agent_async(
                prompt=job["prompt"],
                callback_url=f"{self.public_base_url}/api/worker-callbacks/{job_id}",
                callback_api_key=token,
                run_id=job_id,
                workspace_dir=workspace.get("workspace_dir") if workspace else None,
                system=NO_ASK_SYSTEM_PROMPT,
                model=job.get("model") or None,
                session_id=job.get("thclaws_session_id") or None,
            )
        except ThClawsError as exc:
            # Redact FIRST: a ThClawsError carries the worker/proxy response body, which can
            # ECHO the request — including the x_callback api_key — and both failure paths
            # persist the string (jobs.error / events / audit). Then split by certainty:
            # an HTTP error means the worker ANSWERED and rejected the dispatch before any run
            # started (safe to fail the job); anything else (connect/timeout/reset while
            # sending or reading the ACK) is ambiguous — the worker may have accepted and be
            # running with a valid token, so the job must stay callback-pending.
            message = str(exc).replace(token, "[redacted-callback-token]")
            definitive_rejection = (
                exc.request_not_accepted  # connection refused / DNS: provably never delivered
                # 4xx = the worker VALIDATED and rejected the dispatch before spawning a run.
                # 5xx stays ambiguous: a proxy 502/504 (or a worker 500) can arrive AFTER the
                # request was accepted and the run scheduled — failing the job then would
                # discard the run's future callback.
                or (exc.http_status is not None and 400 <= exc.http_status < 500)
            )
            if definitive_rejection:
                raise ThClawsError(message, http_status=exc.http_status) from None
            raise _CallbackDispatchUnconfirmed(message) from None
        try:
            session_id = ack.get("session_id")
            # A session id that contains the callback token is a semi-trusted worker trying to
            # get the LIVE credential persisted (thclaws_session_id / session event / binding),
            # where a read-authorized user could retrieve it and forge the terminal callback.
            # A real session id never contains the token, so skip binding rather than store it.
            if isinstance(session_id, str) and session_id and token not in session_id:
                self._record_session(job, worker, workspace, session_id)
            self.db.append_job_event(job_id, "callback_dispatched", {"deadline_at": deadline_at, "run_id": job_id})
        except Exception:
            # The worker accepted (202) and is running: an internal bookkeeping failure must
            # not fail the job out from under a run that will deliver a real result.
            LOGGER.exception("post-ACK bookkeeping failed for callback job %s", job_id)

    def apply_worker_callback(self, job_id: str, payload: dict[str, Any], token: str | None = None) -> dict[str, Any]:
        """Apply a worker's terminal callback payload IDEMPOTENTLY. The atomic
        apply_job_terminal_result transition picks exactly one winner, so a duplicate delivery (thClaws
        retries at ~0/10/60s), or a callback racing the deadline reaper, converges to a single
        terminal state — the loser applies nothing and returns applied:false with 200 (any
        non-429 4xx would make the worker abandon a delivery that already succeeded). `token`
        is the verified callback Bearer, passed so it can be redacted from any stored error."""
        job = self.db.get_job(job_id)
        if not job:
            raise FileNotFoundError()
        if (job.get("execution") or "stream") != "callback":
            raise ValueError("job does not use callback execution")
        body_run_id = payload.get("run_id")
        if not (isinstance(body_run_id, str) and body_run_id == job_id):
            # The documented payload REQUIRES run_id == the Atlas job id (real thClaws always
            # sends it). Require an exact nonempty-string match: a missing / null / empty /
            # non-string / mismatching run_id is a delivery mix-up or a malformed body, and
            # applying run B's result to job A would be irreversible. 400 is correct — thClaws
            # will not retry a non-429 4xx, so a genuine mix-up is not re-attempted into A.
            raise ValueError("callback run_id must equal the job id in the URL")
        status = payload.get("status")
        # isinstance first: an unhashable status (array/object) must map to 'failed', not
        # TypeError out of the set probe (a 500 that leaves the job running forever).
        state = status if isinstance(status, str) and status in {"succeeded", "cancelled"} else "failed"
        error: str | None = None
        if state == "failed":
            detail = payload.get("error")
            message = detail.get("message") if isinstance(detail, dict) else None
            error = str(message or f"worker reported status: {status}")[:4096]
            # error.message is worker-controlled; if it reflects the callback api_key (the
            # Bearer it received), persisting it verbatim breaks the never-store-tokens
            # invariant. Redacted below, together with every other worker-controlled field.
            error = _redact_token(error, token)
        raw_usage = payload.get("usage")
        usage = extract_usage(SseEvent(event="usage", data=json.dumps(raw_usage))) if isinstance(raw_usage, dict) else None
        raw_summary = payload.get("summary")
        # summary → assistant_text; tool names → the callback_result event. Both are
        # worker-controlled and could echo the LIVE callback token, so redact before storage.
        summary = _redact_token(raw_summary, token) if isinstance(raw_summary, str) and raw_summary else None
        finished_at = now_iso()
        # ONE transaction applies the terminal state + summary text (stored regardless of
        # outcome, matching stream semantics) + the structural callback_result event (tool
        # NAMES and counters only — the callback shape carries no tool input/output, per T2's
        # projection rule) + audit + the usage ledger row. Atomicity is what preserves the
        # worker's RETRY as a recovery mechanism: a crash mid-apply leaves the job
        # non-terminal, so the retry re-applies everything instead of hitting an
        # already-terminal job and losing the result. A racing cancel atomically wins inside
        # the same UPDATE (the worker has no cancel endpoint, so its result arrives anyway).
        final_state = self.db.apply_job_terminal_result(
            job_id,
            state,
            finished_at=finished_at,
            error=error,
            summary=summary,
            events=[("callback_result", _project_callback_result(payload, token))],
            usage_payload=self._usage_payload(job, usage, finished_at),
        )
        if final_state is None:
            # Lost the terminal race (duplicate delivery / reaper). Normally a no-op — but if
            # Atlas crashed after the terminal commit and BEFORE _maybe_start_handoff, the job
            # is succeeded with a configured-but-UNRESOLVED handoff, and the worker's retry is
            # the only recovery signal. Recover ONLY when the handoff genuinely never ran:
            # neither started (handoff_job_id) NOR already resolved (handoff_error, set when it
            # was skipped for empty text or failed). Without the handoff_error guard, every
            # duplicate replay of a skipped/failed handoff would re-enter _maybe_start_handoff
            # and append another event/audit row — an unbounded write a token-holder could
            # exploit. A resolved handoff stays resolved.
            current = self.db.get_job(job_id) or job
            if (
                current.get("state") == "succeeded"
                and not current.get("handoff_job_id")
                and not current.get("handoff_error")
                and (current.get("handoff_worker_id") or current.get("handoff_workspace_id"))
            ):
                self._maybe_start_handoff(job_id)
            return {"applied": False, "state": current.get("state")}
        if final_state == "succeeded":
            self._maybe_start_handoff(job_id)
        return {"applied": True, "state": final_state}

    def reap_callback_jobs(self) -> int:
        """Fail (or cancel, if a cancel was requested) callback jobs whose deadline passed —
        AND whose retry-envelope grace has also elapsed, so a worker's in-flight retry (whose
        token is still valid) is never terminal-ized out from under it. Uses the same atomic
        apply_job_terminal_result as apply_worker_callback, so a callback landing mid-sweep
        converges to one terminal state. Returns how many jobs this sweep terminal-ized."""
        # Cutoff = now - grace: a job is reapable only once its deadline is older than the full
        # retry envelope, matching the token's lifetime (dispatch + timeout + envelope).
        cutoff = (datetime.now(UTC) - timedelta(seconds=self.callback_reap_grace_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
        reaped = 0
        for job in self.db.list_due_callback_jobs(cutoff):
            error = f"no worker callback before deadline {job.get('callback_deadline_at')}"
            finished_at = now_iso()
            # Single-transaction apply; the cancel_requested CASE converts to 'cancelled'
            # (NULL error) atomically when the user cancelled — no pre-read needed.
            with self.db.as_actor("system:callback-reaper"):
                final = self.db.apply_job_terminal_result(
                    job["id"],
                    "failed",
                    finished_at=finished_at,
                    error=error,
                    state_reason="callback_deadline_exceeded",
                    audit_details={"reason": "callback_deadline_exceeded"},
                    usage_payload=self._usage_payload(job, None, finished_at),
                )
            if final is None:
                continue  # a callback won the race — already terminal, nothing to apply
            reaped += 1
        return reaped

    def start_callback_reaper(self, interval_seconds: float | None = None) -> threading.Thread:
        """Background sweep for callback jobs that never call back. Daemon thread, hermetic to
        this manager; checks call reap_callback_jobs() directly for determinism."""
        interval = float(os.getenv("ATLAS_CALLBACK_REAPER_INTERVAL_SECONDS", "5")) if interval_seconds is None else interval_seconds

        def _loop() -> None:
            while True:
                time.sleep(interval)
                try:
                    self.reap_callback_jobs()
                except Exception:
                    LOGGER.exception("callback reaper sweep failed")

        thread = threading.Thread(target=_loop, name="atlas-callback-reaper", daemon=True)
        thread.start()
        return thread

    def _usage_payload(self, job: dict[str, Any], usage: dict[str, int] | None, finished_at: str | None) -> dict[str, Any]:
        """Build the usage_events row for one terminal job. `status` is patched to the FINAL
        terminal state by apply_job_terminal_result when used in the atomic apply path."""
        context = self.db.workflow_context_for_job(job["id"])
        seconds = elapsed_seconds(job.get("started_at"), finished_at)
        return {
            "idempotency_key": f"job:{job['id']}",
            "kind": "job",
            "run_id": context.get("run_id"),
            "job_id": job["id"],
            "node_key": context.get("node_key"),
            "worker_id": job.get("worker_id"),
            "status": job.get("state"),
            "units": 1,
            "seconds": seconds,
            "started_at": job.get("started_at"),
            "finished_at": finished_at,
            "model": job.get("model") or None,
            "tokens_prompt": usage.get("prompt_tokens") if usage else None,
            "tokens_output": usage.get("completion_tokens") if usage else None,
            "metadata": {
                "measures": {
                    "workflow_run_count": 0,
                    "job_count": 1,
                    "budget_units": 0,
                    "wall_seconds": seconds,
                    # Full usage payload (cached/creation/reasoning counts included).
                    **(usage or {}),
                },
                "byok_token_counts_billable": False,
            },
        }

    def _record_job_usage(self, job_id: str, usage: dict[str, int] | None = None) -> None:
        try:
            job = self.db.get_job(job_id)
            if not job or job.get("state") not in TERMINAL_STATES:
                return
            self.db.emit_usage_event(self._usage_payload(job, usage, job.get("finished_at")))
        except Exception:
            LOGGER.exception("usage metering failed for job %s", job_id)

    def _maybe_start_handoff(self, source_job_id: str) -> None:
        # Serialize the check-then-submit under the manager lock with a re-read INSIDE it: two
        # concurrent duplicate callbacks (both losing the terminal race) can both reach the
        # replay-recovery path and race here, and a plain check-then-submit would start two
        # distinct child jobs — duplicate worker side effects. Under the one-writer-per-DB
        # deployment model (threat-model; same basis as claim_trigger_dedupe's in-process
        # atomicity) this lock makes the claim atomic: the first caller sets handoff_job_id,
        # every other observes it and returns. RLock is reentrant, so submit()'s own lock use
        # on this thread is fine.
        with self._lock:
            source = self.db.get_job(source_job_id)
            if not source or source.get("handoff_job_id"):
                return
            target_worker_id = source.get("handoff_worker_id")
            target_workspace_id = source.get("handoff_workspace_id")
            prompt_template = source.get("handoff_prompt") or ""
            if not target_worker_id and not target_workspace_id:
                return
            self._start_handoff_locked(source_job_id, source, target_worker_id, target_workspace_id, prompt_template)

    def _start_handoff_locked(
        self,
        source_job_id: str,
        source: dict[str, Any],
        target_worker_id: str | None,
        target_workspace_id: str | None,
        prompt_template: str,
    ) -> None:
        # DETERMINISTIC child id derived from the source: this closes the crash window the
        # in-process lock cannot. If Atlas crashes after submit() created the child but before
        # handoff_job_id is written, the worker's retry re-enters here and finds the SAME child
        # already exists — it links it instead of starting a second run. (The lock still
        # serializes live duplicate callers so only one create happens per process.)
        child_id = _handoff_child_id(source_job_id)
        existing = self.db.get_job(child_id)
        if existing:
            if not source.get("handoff_job_id"):
                self.db.update_job(source_job_id, handoff_job_id=child_id, handoff_error=None)
                self.db.append_job_event(
                    source_job_id, "handoff_started",
                    {"job_id": child_id, "worker_id": existing.get("worker_id"), "recovered": True},
                )
                self.db.audit("job.handoff_started", "job", source_job_id, {"child_job_id": child_id, "recovered": True})
            return

        result = source.get("assistant_text") or ""
        if not result.strip():
            message = "handoff skipped because source job produced no assistant text"
            self.db.update_job(source_job_id, handoff_error=message)
            self.db.append_job_event(source_job_id, "handoff_skipped", {"error": message})
            return

        prompt = self._render_handoff_prompt(prompt_template, source, result)
        try:
            child = self.submit(
                {
                    "prompt": prompt,
                    "conversation_id": source.get("conversation_id"),
                    "worker_id": target_worker_id,
                    "workspace_id": target_workspace_id,
                    "parent_job_id": source_job_id,
                    "model": source.get("model") or "",
                },
                explicit_id=child_id,
            )
            self.db.update_job(source_job_id, handoff_job_id=child["id"], handoff_error=None)
            self.db.append_job_event(
                source_job_id,
                "handoff_started",
                {
                    "job_id": child["id"],
                    "worker_id": child.get("worker_id"),
                    "workspace_id": child.get("workspace_id"),
                },
            )
            self.db.audit("job.handoff_started", "job", source_job_id, {"child_job_id": child["id"]})
        except Exception as exc:
            self.db.update_job(source_job_id, handoff_error=str(exc))
            self.db.append_job_event(source_job_id, "handoff_error", {"error": str(exc)})
            self.db.audit("job.handoff_failed", "job", source_job_id, {"error": str(exc)})

    @staticmethod
    def _render_handoff_prompt(template: str, source: dict[str, Any], result: str) -> str:
        if "{result}" not in template:
            template = f"{template.rstrip()}\n\n{{result}}"
        return (
            template.replace("{result}", result)
            .replace("{source_prompt}", source.get("prompt") or "")
            .replace("{source_job_id}", source.get("id") or "")
        )


class _JobCancelled(Exception):
    pass


class _CallbackDispatchUnconfirmed(Exception):
    """The x_callback POST failed AMBIGUOUSLY (connect/timeout/reset while sending or reading
    the 202 ACK): the worker may have accepted the run and hold a valid callback token. The
    dispatch thread must leave the job callback-pending — the callback or the deadline reaper
    resolves it — because failing it would discard a legitimately delivered future result."""
