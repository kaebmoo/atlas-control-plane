from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def encode_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def decode_json(value: str | None, fallback: Any = None) -> Any:
    if value in (None, ""):
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    for key in ("tags", "metadata", "agent_info", "payload", "details"):
        if key in data:
            data[key] = decode_json(data[key], [] if key == "tags" else {})
    return data


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS workers (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  base_url TEXT NOT NULL UNIQUE,
  token TEXT,
  role TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'unknown',
  last_seen_at TEXT,
  agent_info TEXT NOT NULL DEFAULT '{}',
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspaces (
  id TEXT PRIMARY KEY,
  worker_id TEXT NOT NULL,
  workspace_key TEXT NOT NULL,
  workspace_dir TEXT NOT NULL,
  company TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(worker_id, workspace_key),
  FOREIGN KEY(worker_id) REFERENCES workers(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  preferred_worker_id TEXT,
  preferred_workspace_id TEXT,
  workspace_key TEXT NOT NULL DEFAULT '',
  company TEXT NOT NULL DEFAULT '',
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(preferred_worker_id) REFERENCES workers(id) ON DELETE SET NULL,
  FOREIGN KEY(preferred_workspace_id) REFERENCES workspaces(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS session_bindings (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  worker_id TEXT NOT NULL,
  workspace_id TEXT,
  thclaws_session_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(conversation_id, worker_id, workspace_id),
  FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
  FOREIGN KEY(worker_id) REFERENCES workers(id) ON DELETE CASCADE,
  FOREIGN KEY(workspace_id) REFERENCES workspaces(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  conversation_id TEXT,
  worker_id TEXT NOT NULL,
  workspace_id TEXT,
  parent_job_id TEXT,
  state TEXT NOT NULL,
  prompt TEXT NOT NULL,
  model TEXT NOT NULL DEFAULT '',
  route_reason TEXT NOT NULL DEFAULT '',
  thclaws_session_id TEXT,
  assistant_text TEXT NOT NULL DEFAULT '',
  error TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  handoff_worker_id TEXT,
  handoff_workspace_id TEXT,
  handoff_prompt TEXT NOT NULL DEFAULT '',
  handoff_job_id TEXT,
  handoff_error TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE SET NULL,
  FOREIGN KEY(worker_id) REFERENCES workers(id) ON DELETE CASCADE,
  FOREIGN KEY(workspace_id) REFERENCES workspaces(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS job_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  text TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(job_id, seq),
  FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_job_events_job_seq ON job_events(job_id, seq);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  action TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT 'local',
  resource_type TEXT NOT NULL,
  resource_id TEXT NOT NULL,
  details TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self._lock, self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        migrations = {
            "parent_job_id": "ALTER TABLE jobs ADD COLUMN parent_job_id TEXT",
            "handoff_worker_id": "ALTER TABLE jobs ADD COLUMN handoff_worker_id TEXT",
            "handoff_workspace_id": "ALTER TABLE jobs ADD COLUMN handoff_workspace_id TEXT",
            "handoff_prompt": "ALTER TABLE jobs ADD COLUMN handoff_prompt TEXT NOT NULL DEFAULT ''",
            "handoff_job_id": "ALTER TABLE jobs ADD COLUMN handoff_job_id TEXT",
            "handoff_error": "ALTER TABLE jobs ADD COLUMN handoff_error TEXT",
        }
        for column, sql in migrations.items():
            if column not in columns:
                conn.execute(sql)

    def audit(self, action: str, resource_type: str, resource_id: str, details: Any = None, actor: str = "local") -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_log(action, actor, resource_type, resource_id, details, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (action, actor, resource_type, resource_id, encode_json(details or {}), now_iso()),
            )

    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def upsert_worker(self, payload: dict[str, Any]) -> dict[str, Any]:
        worker_id = payload.get("id") or new_id("wrk")
        now = now_iso()
        tags = payload.get("tags") or []
        if isinstance(tags, str):
            tags = [tag.strip() for tag in tags.split(",") if tag.strip()]
        base_url = str(payload["base_url"]).rstrip("/")
        with self._lock, self.connect() as conn:
            existing = conn.execute("SELECT id, created_at, token FROM workers WHERE id = ? OR base_url = ?", (worker_id, base_url)).fetchone()
            if existing:
                worker_id = existing["id"]
                created_at = existing["created_at"]
            else:
                created_at = now
            token = payload.get("token")
            if token is None or str(token).strip() == "":
                token = existing["token"] if existing else None
            conn.execute(
                """
                INSERT INTO workers(id, name, base_url, token, role, tags, status, last_seen_at, agent_info, last_error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  name=excluded.name,
                  base_url=excluded.base_url,
                  token=excluded.token,
                  role=excluded.role,
                  tags=excluded.tags,
                  updated_at=excluded.updated_at
                """,
                (
                    worker_id,
                    payload.get("name") or base_url,
                    base_url,
                    token,
                    payload.get("role") or "",
                    encode_json(tags),
                    payload.get("status") or "unknown",
                    payload.get("last_seen_at"),
                    encode_json(payload.get("agent_info") or {}),
                    payload.get("last_error"),
                    created_at,
                    now,
                ),
            )
        self.audit("worker.upsert", "worker", worker_id, {"base_url": base_url})
        return self.get_worker(worker_id) or {}

    def update_worker_status(self, worker_id: str, status: str, agent_info: Any = None, error: str | None = None) -> None:
        with self._lock, self.connect() as conn:
            previous = conn.execute("SELECT status, last_error FROM workers WHERE id = ?", (worker_id,)).fetchone()
            conn.execute(
                """
                UPDATE workers
                SET status = ?, last_seen_at = ?, agent_info = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, now_iso() if status in {"online", "healthy"} else None, encode_json(agent_info or {}), error, now_iso(), worker_id),
            )
        if not previous or previous["status"] != status or previous["last_error"] != error:
            self.audit("worker.poll", "worker", worker_id, {"status": status, "error": error})

    def get_worker(self, worker_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM workers WHERE id = ?", (worker_id,)).fetchone()
        return row_to_dict(row)

    def list_workers(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM workers ORDER BY name COLLATE NOCASE").fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def delete_worker(self, worker_id: str) -> bool:
        with self._lock, self.connect() as conn:
            cursor = conn.execute("DELETE FROM workers WHERE id = ?", (worker_id,))
        deleted = cursor.rowcount > 0
        if deleted:
            self.audit("worker.delete", "worker", worker_id)
        return deleted

    def delete_workspace(self, workspace_id: str) -> bool:
        with self._lock, self.connect() as conn:
            cursor = conn.execute("DELETE FROM workspaces WHERE id = ?", (workspace_id,))
        deleted = cursor.rowcount > 0
        if deleted:
            self.audit("workspace.delete", "workspace", workspace_id)
        return deleted

    def upsert_workspace(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace_id = payload.get("id") or new_id("wsp")
        now = now_iso()
        tags = payload.get("tags") or []
        if isinstance(tags, str):
            tags = [tag.strip() for tag in tags.split(",") if tag.strip()]
        with self._lock, self.connect() as conn:
            existing = conn.execute(
                "SELECT id, created_at FROM workspaces WHERE id = ? OR (worker_id = ? AND workspace_key = ?)",
                (workspace_id, payload["worker_id"], payload["workspace_key"]),
            ).fetchone()
            if existing:
                workspace_id = existing["id"]
                created_at = existing["created_at"]
            else:
                created_at = now
            conn.execute(
                """
                INSERT INTO workspaces(id, worker_id, workspace_key, workspace_dir, company, tags, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  worker_id=excluded.worker_id,
                  workspace_key=excluded.workspace_key,
                  workspace_dir=excluded.workspace_dir,
                  company=excluded.company,
                  tags=excluded.tags,
                  updated_at=excluded.updated_at
                """,
                (
                    workspace_id,
                    payload["worker_id"],
                    payload["workspace_key"],
                    payload["workspace_dir"],
                    payload.get("company") or "",
                    encode_json(tags),
                    created_at,
                    now,
                ),
            )
        self.audit("workspace.upsert", "workspace", workspace_id, {"workspace_key": payload["workspace_key"]})
        return self.get_workspace(workspace_id) or {}

    def get_workspace(self, workspace_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        return row_to_dict(row)

    def list_workspaces(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT workspaces.*, workers.name AS worker_name, workers.status AS worker_status
                FROM workspaces
                JOIN workers ON workers.id = workspaces.worker_id
                ORDER BY company COLLATE NOCASE, workspace_key COLLATE NOCASE
                """
            ).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def create_conversation(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        conversation_id = payload.get("id") or new_id("cnv")
        now = now_iso()
        title = payload.get("title") or "Untitled"
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO conversations(id, title, preferred_worker_id, preferred_workspace_id, workspace_key, company, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    title,
                    payload.get("preferred_worker_id"),
                    payload.get("preferred_workspace_id"),
                    payload.get("workspace_key") or "",
                    payload.get("company") or "",
                    encode_json(payload.get("metadata") or {}),
                    now,
                    now,
                ),
            )
        self.audit("conversation.create", "conversation", conversation_id, {"title": title})
        return self.get_conversation(conversation_id) or {}

    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        return row_to_dict(row)

    def list_conversations(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM conversations ORDER BY updated_at DESC LIMIT 100").fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def find_session_binding(self, conversation_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM session_bindings
                WHERE conversation_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
        return row_to_dict(row)

    def upsert_session_binding(self, conversation_id: str, worker_id: str, workspace_id: str | None, thclaws_session_id: str) -> None:
        binding_id = new_id("ses")
        now = now_iso()
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO session_bindings(id, conversation_id, worker_id, workspace_id, thclaws_session_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id, worker_id, workspace_id) DO UPDATE SET
                  thclaws_session_id=excluded.thclaws_session_id,
                  updated_at=excluded.updated_at
                """,
                (binding_id, conversation_id, worker_id, workspace_id, thclaws_session_id, now, now),
            )
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )
        self.audit("session.bind", "conversation", conversation_id, {"worker_id": worker_id, "workspace_id": workspace_id})

    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        job_id = payload.get("id") or new_id("job")
        now = now_iso()
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs(
                  id, conversation_id, worker_id, workspace_id, parent_job_id, state,
                  prompt, model, route_reason, thclaws_session_id,
                  handoff_worker_id, handoff_workspace_id, handoff_prompt,
                  created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    payload.get("conversation_id"),
                    payload["worker_id"],
                    payload.get("workspace_id"),
                    payload.get("parent_job_id"),
                    payload.get("state") or "queued",
                    payload["prompt"],
                    payload.get("model") or "",
                    payload.get("route_reason") or "",
                    payload.get("thclaws_session_id"),
                    payload.get("handoff_worker_id"),
                    payload.get("handoff_workspace_id"),
                    payload.get("handoff_prompt") or "",
                    now,
                    now,
                ),
            )
        self.audit(
            "job.create",
            "job",
            job_id,
            {
                "worker_id": payload["worker_id"],
                "workspace_id": payload.get("workspace_id"),
                "parent_job_id": payload.get("parent_job_id"),
            },
        )
        return self.get_job(job_id) or {}

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now_iso()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [job_id]
        with self._lock, self.connect() as conn:
            conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", values)

    def mark_cancel_requested(self, job_id: str) -> None:
        self.update_job(job_id, cancel_requested=1, state="cancel_requested")
        self.audit("job.cancel_requested", "job", job_id)

    def append_job_event(self, job_id: str, event_type: str, payload: Any = None, text: str | None = None) -> dict[str, Any]:
        with self._lock, self.connect() as conn:
            row = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM job_events WHERE job_id = ?", (job_id,)).fetchone()
            seq = int(row["next_seq"])
            conn.execute(
                """
                INSERT INTO job_events(job_id, seq, event_type, payload, text, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (job_id, seq, event_type, encode_json(payload or {}), text, now_iso()),
            )
        return {"job_id": job_id, "seq": seq, "event_type": event_type, "payload": payload or {}, "text": text, "created_at": now_iso()}

    def append_job_text(self, job_id: str, text: str) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET assistant_text = assistant_text || ?, updated_at = ?
                WHERE id = ?
                """,
                (text, now_iso(), job_id),
            )
        self.append_job_event(job_id, "text", {"text": text}, text=text)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return row_to_dict(row)

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT jobs.*, workers.name AS worker_name, workspaces.workspace_key AS workspace_key
                FROM jobs
                JOIN workers ON workers.id = jobs.worker_id
                LEFT JOIN workspaces ON workspaces.id = jobs.workspace_id
                ORDER BY jobs.created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def get_job_events_after(self, job_id: str, after_seq: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM job_events
                WHERE job_id = ? AND seq > ?
                ORDER BY seq ASC
                LIMIT ?
                """,
                (job_id, after_seq, limit),
            ).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def is_cancel_requested(self, job_id: str) -> bool:
        job = self.get_job(job_id)
        return bool(job and job.get("cancel_requested"))
