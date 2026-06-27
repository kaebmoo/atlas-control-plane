from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .db import Database


@dataclass(frozen=True)
class RouteDecision:
    worker: dict[str, Any]
    workspace: dict[str, Any] | None
    reason: str
    thclaws_session_id: str | None = None


class Router:
    def __init__(self, db: Database):
        self.db = db

    def resolve(self, payload: dict[str, Any]) -> RouteDecision:
        workers = self.db.list_workers()
        workspaces = self.db.list_workspaces()
        raw_allowed_worker_ids = payload.get("allowed_worker_ids")
        if raw_allowed_worker_ids is not None and (
            not isinstance(raw_allowed_worker_ids, list)
            or not all(isinstance(worker_id, str) and worker_id for worker_id in raw_allowed_worker_ids)
        ):
            raise ValueError("allowed_worker_ids must be a list of ids")
        allowed_worker_ids = set(raw_allowed_worker_ids or [])
        if allowed_worker_ids:
            workers = [worker for worker in workers if worker["id"] in allowed_worker_ids]
        if not workers:
            raise ValueError("No workers registered")

        requested_workspace_id = payload.get("workspace_id")
        requested_worker_id = payload.get("worker_id")
        conversation_id = payload.get("conversation_id")

        if requested_workspace_id:
            workspace = self._workspace_by_id(workspaces, requested_workspace_id)
            if not workspace:
                raise ValueError(f"Unknown workspace_id: {requested_workspace_id}")
            worker = self._worker_by_id(workers, workspace["worker_id"])
            if not worker:
                raise ValueError(f"Workspace worker is missing: {workspace['worker_id']}")
            return RouteDecision(worker=worker, workspace=workspace, reason="explicit workspace")

        if requested_worker_id:
            worker = self._worker_by_id(workers, requested_worker_id)
            if not worker:
                raise ValueError(f"Unknown worker_id: {requested_worker_id}")
            workspace = self._best_workspace_for_worker(workspaces, worker["id"], payload)
            return RouteDecision(worker=worker, workspace=workspace, reason="explicit worker")

        if conversation_id:
            binding = self.db.find_session_binding(conversation_id)
            if binding:
                worker = self._worker_by_id(workers, binding["worker_id"])
                workspace = self._workspace_by_id(workspaces, binding.get("workspace_id") or "")
                if worker:
                    return RouteDecision(
                        worker=worker,
                        workspace=workspace,
                        reason="existing conversation session binding",
                        thclaws_session_id=binding.get("thclaws_session_id"),
                    )

        ranked = self._rank_candidates(workers, workspaces, payload)
        if not ranked:
            raise ValueError("No routeable worker/workspace candidates")
        score, reason, worker, workspace = ranked[0]
        return RouteDecision(worker=worker, workspace=workspace, reason=f"{reason} (score {score})")

    def _rank_candidates(
        self,
        workers: list[dict[str, Any]],
        workspaces: list[dict[str, Any]],
        payload: dict[str, Any],
    ) -> list[tuple[int, str, dict[str, Any], dict[str, Any] | None]]:
        workspace_key = (payload.get("workspace_key") or "").lower()
        company = (payload.get("company") or "").lower()
        prompt = (payload.get("prompt") or "").lower()
        requested_role = (payload.get("role") or "").lower()
        requested_tags = set(_normalize_tags(payload.get("tags") or []))

        candidates: list[tuple[int, str, dict[str, Any], dict[str, Any] | None]] = []
        for worker in workers:
            worker_tags = set(_normalize_tags(worker.get("tags") or []))
            role = str(worker.get("role") or "").lower()
            if requested_role and requested_role not in worker_tags | {role}:
                continue
            worker_workspaces = [workspace for workspace in workspaces if workspace["worker_id"] == worker["id"]]
            if not worker_workspaces:
                worker_workspaces = [None]
            for workspace in worker_workspaces:
                score = 0
                reasons: list[str] = []
                status = worker.get("status")
                if status in {"online", "healthy"}:
                    score += 30
                    reasons.append("online worker")
                elif status == "unknown":
                    score += 5
                    reasons.append("unknown worker")
                else:
                    score -= 50
                    reasons.append("offline worker")

                workspace_tags = set(_normalize_tags((workspace or {}).get("tags") or []))
                all_tags = worker_tags | workspace_tags
                tag_hits = requested_tags & all_tags
                if tag_hits:
                    score += 10 * len(tag_hits)
                    reasons.append(f"tag match: {', '.join(sorted(tag_hits))}")

                if workspace:
                    if workspace_key and workspace_key == str(workspace.get("workspace_key", "")).lower():
                        score += 45
                        reasons.append("workspace key match")
                    if company and company == str(workspace.get("company", "")).lower():
                        score += 35
                        reasons.append("company match")
                    text_blob = " ".join(
                        [
                            str(workspace.get("workspace_key", "")),
                            str(workspace.get("company", "")),
                            " ".join(workspace_tags),
                        ]
                    ).lower()
                    if prompt and any(part and part in prompt for part in text_blob.split()):
                        score += 5
                        reasons.append("prompt hints")

                if requested_role:
                    score += 50
                    reasons.append("role match")
                if role and role in prompt:
                    score += 15
                    reasons.append("role hint")

                reason = ", ".join(reasons) if reasons else "fallback"
                candidates.append((score, reason, worker, workspace))
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates

    @staticmethod
    def _worker_by_id(workers: list[dict[str, Any]], worker_id: str) -> dict[str, Any] | None:
        return next((worker for worker in workers if worker["id"] == worker_id), None)

    @staticmethod
    def _workspace_by_id(workspaces: list[dict[str, Any]], workspace_id: str) -> dict[str, Any] | None:
        return next((workspace for workspace in workspaces if workspace["id"] == workspace_id), None)

    @staticmethod
    def _best_workspace_for_worker(workspaces: list[dict[str, Any]], worker_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        owned = [workspace for workspace in workspaces if workspace["worker_id"] == worker_id]
        if not owned:
            return None
        workspace_key = payload.get("workspace_key")
        company = payload.get("company")
        if workspace_key:
            match = next((workspace for workspace in owned if workspace["workspace_key"] == workspace_key), None)
            if match:
                return match
        if company:
            match = next((workspace for workspace in owned if workspace["company"] == company), None)
            if match:
                return match
        return owned[0]


def _normalize_tags(tags: Any) -> list[str]:
    if isinstance(tags, str):
        tags = tags.split(",")
    if not isinstance(tags, list):
        return []
    return [str(tag).strip().lower() for tag in tags if str(tag).strip()]
