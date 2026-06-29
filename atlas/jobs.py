from __future__ import annotations

import contextvars
import logging
import threading
from typing import Any

from .db import Database, now_iso
from .router import Router
from .thclaws_client import ThClawsClient, ThClawsError, extract_session_id, extract_text, parse_event_payload
from .usage import elapsed_seconds


TERMINAL_STATES = {"succeeded", "failed", "cancelled"}
DEFAULT_HANDOFF_PROMPT = """คุณได้รับผลลัพธ์จาก agent ก่อนหน้า

งานของคุณคือเรียบเรียงผลลัพธ์นี้ให้พร้อมส่งต่อผู้ใช้ โดยรักษาข้อเท็จจริง ไม่แต่งเติมข้อมูลที่ไม่มีในต้นฉบับ

ผลลัพธ์จาก agent ก่อนหน้า:
{result}
"""
LOGGER = logging.getLogger(__name__)


class JobManager:
    def __init__(self, db: Database, request_timeout_seconds: float = 30):
        self.db = db
        self.router = Router(db)
        self.request_timeout_seconds = request_timeout_seconds
        self.trigger_service: Any = None
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("prompt is required")

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
        if job["state"] in TERMINAL_STATES:
            return job
        self.db.mark_cancel_requested(job_id)
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

        self.db.update_job(job_id, state="running", started_at=now_iso())
        self.db.append_job_event(job_id, "state", {"state": "running"})
        client = ThClawsClient(worker["base_url"], worker.get("token"), timeout=self.request_timeout_seconds)
        done_seen = False
        try:
            for event in client.run_agent_stream(
                prompt=job["prompt"],
                workspace_dir=workspace.get("workspace_dir") if workspace else None,
                model=job.get("model") or None,
                session_id=job.get("thclaws_session_id") or None,
            ):
                if self.db.is_cancel_requested(job_id):
                    raise _JobCancelled()

                payload = parse_event_payload(event)
                if event.data == "[DONE]":
                    self.db.append_job_event(job_id, "done", payload)
                    done_seen = True
                    break

                session_id = extract_session_id(event)
                if session_id:
                    self.db.update_job(job_id, thclaws_session_id=session_id)
                    if job.get("conversation_id"):
                        self.db.upsert_session_binding(
                            job["conversation_id"],
                            worker["id"],
                            workspace["id"] if workspace else None,
                            session_id,
                        )
                    self.db.append_job_event(job_id, "session", {"session_id": session_id})
                    continue

                text = extract_text(event)
                if text:
                    self.db.append_job_text(job_id, text)
                else:
                    self.db.append_job_event(job_id, event.event or "message", payload)

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
            self._record_job_usage(job_id)
            with self._lock:
                self._threads.pop(job_id, None)

    def _record_job_usage(self, job_id: str) -> None:
        try:
            job = self.db.get_job(job_id)
            if not job or job.get("state") not in TERMINAL_STATES:
                return
            context = self.db.workflow_context_for_job(job_id)
            seconds = elapsed_seconds(job.get("started_at"), job.get("finished_at"))
            self.db.emit_usage_event(
                {
                    "idempotency_key": f"job:{job_id}",
                    "kind": "job",
                    "run_id": context.get("run_id"),
                    "job_id": job_id,
                    "node_key": context.get("node_key"),
                    "worker_id": job.get("worker_id"),
                    "status": job.get("state"),
                    "units": 1,
                    "seconds": seconds,
                    "started_at": job.get("started_at"),
                    "finished_at": job.get("finished_at"),
                    "model": job.get("model") or None,
                    "metadata": {
                        "measures": {
                            "workflow_run_count": 0,
                            "job_count": 1,
                            "budget_units": 0,
                            "wall_seconds": seconds,
                        },
                        "byok_token_counts_billable": False,
                    },
                }
            )
        except Exception:
            LOGGER.exception("usage metering failed for job %s", job_id)

    def _maybe_start_handoff(self, source_job_id: str) -> None:
        source = self.db.get_job(source_job_id)
        if not source or source.get("handoff_job_id"):
            return
        target_worker_id = source.get("handoff_worker_id")
        target_workspace_id = source.get("handoff_workspace_id")
        prompt_template = source.get("handoff_prompt") or ""
        if not target_worker_id and not target_workspace_id:
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
                }
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
