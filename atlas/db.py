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
    list_fields = {"tags", "current_nodes", "input_artifacts", "output_artifacts"}
    json_fields = {
        "agent_info",
        "condition_result",
        "config",
        "counters",
        "details",
        "graph",
        "input",
        "metadata",
        "payload",
        "policy",
    }
    for key in list_fields | json_fields:
        if key in data:
            data[key] = decode_json(data[key], [] if key in list_fields else {})
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

CREATE TABLE IF NOT EXISTS workflow_definitions (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  version INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'draft',
  graph TEXT NOT NULL,
  policy TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_runs (
  id TEXT PRIMARY KEY,
  workflow_definition_id TEXT,
  name TEXT NOT NULL,
  state TEXT NOT NULL,
  input TEXT NOT NULL DEFAULT '{}',
  current_nodes TEXT NOT NULL DEFAULT '[]',
  counters TEXT NOT NULL DEFAULT '{}',
  error TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(workflow_definition_id) REFERENCES workflow_definitions(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS workflow_nodes (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  node_key TEXT NOT NULL,
  state TEXT NOT NULL,
  job_id TEXT,
  attempt INTEGER NOT NULL DEFAULT 0,
  input_artifacts TEXT NOT NULL DEFAULT '[]',
  output_artifacts TEXT NOT NULL DEFAULT '[]',
  error TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(run_id) REFERENCES workflow_runs(id) ON DELETE CASCADE,
  FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS workflow_edges (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  from_node TEXT NOT NULL,
  to_node TEXT NOT NULL,
  condition_result TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(run_id) REFERENCES workflow_runs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS workflow_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  node_key TEXT,
  payload TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE(run_id, seq),
  FOREIGN KEY(run_id) REFERENCES workflow_runs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY,
  run_id TEXT,
  job_id TEXT,
  key TEXT NOT NULL,
  kind TEXT NOT NULL,
  content TEXT NOT NULL,
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(run_id) REFERENCES workflow_runs(id) ON DELETE CASCADE,
  FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS workflow_triggers (
  id TEXT PRIMARY KEY,
  workflow_definition_id TEXT NOT NULL,
  name TEXT NOT NULL,
  type TEXT NOT NULL,
  config TEXT NOT NULL DEFAULT '{}',
  enabled INTEGER NOT NULL DEFAULT 1,
  last_fired_at TEXT,
  next_fire_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(workflow_definition_id) REFERENCES workflow_definitions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS workflow_trigger_events (
  id TEXT PRIMARY KEY,
  trigger_id TEXT NOT NULL,
  run_id TEXT,
  payload TEXT NOT NULL DEFAULT '{}',
  state TEXT NOT NULL,
  error TEXT,
  dedupe_key TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(trigger_id) REFERENCES workflow_triggers(id) ON DELETE CASCADE,
  FOREIGN KEY(run_id) REFERENCES workflow_runs(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_workflow_definitions_updated ON workflow_definitions(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_created ON workflow_runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_workflow_nodes_run ON workflow_nodes(run_id);
CREATE INDEX IF NOT EXISTS idx_workflow_edges_run ON workflow_edges(run_id);
CREATE INDEX IF NOT EXISTS idx_workflow_events_run_seq ON workflow_events(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id);
CREATE INDEX IF NOT EXISTS idx_workflow_triggers_definition ON workflow_triggers(workflow_definition_id);
CREATE INDEX IF NOT EXISTS idx_workflow_trigger_events_trigger ON workflow_trigger_events(trigger_id, created_at DESC);

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

    def create_workflow_definition(self, payload: dict[str, Any]) -> dict[str, Any]:
        definition_id = payload.get("id") or new_id("wfd")
        now = now_iso()
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO workflow_definitions(id, name, description, version, status, graph, policy, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    definition_id,
                    payload.get("name") or "Untitled workflow",
                    payload.get("description") or "",
                    int(payload.get("version") or 1),
                    payload.get("status") or "draft",
                    encode_json(payload.get("graph") or {}),
                    encode_json(payload.get("policy") or {}),
                    now,
                    now,
                ),
            )
        self.audit("workflow_definition.create", "workflow_definition", definition_id)
        return self.get_workflow_definition(definition_id) or {}

    def get_workflow_definition(self, definition_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM workflow_definitions WHERE id = ?", (definition_id,)).fetchone()
        return row_to_dict(row)

    def list_workflow_definitions(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM workflow_definitions ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def update_workflow_definition(self, definition_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {
            "name": "name",
            "description": "description",
            "version": "version",
            "status": "status",
            "graph": "graph",
            "policy": "policy",
        }
        fields: dict[str, Any] = {}
        for key, column in allowed.items():
            if key in payload:
                fields[column] = encode_json(payload[key]) if key in {"graph", "policy"} else payload[key]
        if not fields:
            return self.get_workflow_definition(definition_id)
        fields["updated_at"] = now_iso()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._lock, self.connect() as conn:
            cursor = conn.execute(f"UPDATE workflow_definitions SET {assignments} WHERE id = ?", list(fields.values()) + [definition_id])
        if cursor.rowcount:
            self.audit("workflow_definition.update", "workflow_definition", definition_id)
        return self.get_workflow_definition(definition_id)

    def delete_workflow_definition(self, definition_id: str) -> bool:
        with self._lock, self.connect() as conn:
            cursor = conn.execute("DELETE FROM workflow_definitions WHERE id = ?", (definition_id,))
        deleted = cursor.rowcount > 0
        if deleted:
            self.audit("workflow_definition.delete", "workflow_definition", definition_id)
        return deleted

    def create_workflow_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        run_id = payload.get("id") or new_id("wfr")
        now = now_iso()
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO workflow_runs(
                  id, workflow_definition_id, name, state, input, current_nodes,
                  counters, error, created_at, started_at, finished_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    payload.get("workflow_definition_id"),
                    payload.get("name") or "Workflow run",
                    payload.get("state") or "queued",
                    encode_json(payload.get("input") or {}),
                    encode_json(payload.get("current_nodes") or []),
                    encode_json(payload.get("counters") or {}),
                    payload.get("error"),
                    now,
                    payload.get("started_at"),
                    payload.get("finished_at"),
                    now,
                ),
            )
        self.audit("workflow_run.create", "workflow_run", run_id, {"workflow_definition_id": payload.get("workflow_definition_id")})
        self.append_workflow_event(
            run_id,
            "created",
            {"workflow_definition_id": payload.get("workflow_definition_id")},
        )
        return self.get_workflow_run(run_id) or {}

    def get_workflow_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM workflow_runs WHERE id = ?", (run_id,)).fetchone()
        return row_to_dict(row)

    def list_workflow_runs(self, limit: int = 100, workflow_definition_id: str | None = None) -> list[dict[str, Any]]:
        if workflow_definition_id:
            sql = "SELECT * FROM workflow_runs WHERE workflow_definition_id = ? ORDER BY created_at DESC LIMIT ?"
            params: tuple[Any, ...] = (workflow_definition_id, limit)
        else:
            sql = "SELECT * FROM workflow_runs ORDER BY created_at DESC LIMIT ?"
            params = (limit,)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def update_workflow_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        for key in ("input", "current_nodes", "counters"):
            if key in fields:
                fields[key] = encode_json(fields[key])
        fields["updated_at"] = now_iso()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._lock, self.connect() as conn:
            conn.execute(f"UPDATE workflow_runs SET {assignments} WHERE id = ?", list(fields.values()) + [run_id])

    def create_workflow_node(self, payload: dict[str, Any]) -> dict[str, Any]:
        node_id = payload.get("id") or new_id("wfn")
        now = now_iso()
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO workflow_nodes(
                  id, run_id, node_key, state, job_id, attempt, input_artifacts,
                  output_artifacts, error, created_at, started_at, finished_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node_id,
                    payload["run_id"],
                    payload["node_key"],
                    payload.get("state") or "queued",
                    payload.get("job_id"),
                    int(payload.get("attempt") or 0),
                    encode_json(payload.get("input_artifacts") or []),
                    encode_json(payload.get("output_artifacts") or []),
                    payload.get("error"),
                    now,
                    payload.get("started_at"),
                    payload.get("finished_at"),
                    now,
                ),
            )
        return self.get_workflow_node(node_id) or {}

    def get_workflow_node(self, node_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM workflow_nodes WHERE id = ?", (node_id,)).fetchone()
        return row_to_dict(row)

    def list_workflow_nodes(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM workflow_nodes WHERE run_id = ? ORDER BY created_at ASC",
                (run_id,),
            ).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def update_workflow_node(self, node_id: str, **fields: Any) -> None:
        if not fields:
            return
        for key in ("input_artifacts", "output_artifacts"):
            if key in fields:
                fields[key] = encode_json(fields[key])
        fields["updated_at"] = now_iso()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._lock, self.connect() as conn:
            conn.execute(f"UPDATE workflow_nodes SET {assignments} WHERE id = ?", list(fields.values()) + [node_id])

    def append_workflow_edge(self, run_id: str, from_node: str, to_node: str, condition_result: Any = None) -> dict[str, Any]:
        edge_id = new_id("wfe")
        created_at = now_iso()
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO workflow_edges(id, run_id, from_node, to_node, condition_result, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (edge_id, run_id, from_node, to_node, encode_json(condition_result or {}), created_at),
            )
        return {
            "id": edge_id,
            "run_id": run_id,
            "from_node": from_node,
            "to_node": to_node,
            "condition_result": condition_result or {},
            "created_at": created_at,
        }

    def list_workflow_edges(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM workflow_edges WHERE run_id = ? ORDER BY created_at ASC",
                (run_id,),
            ).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def append_workflow_event(
        self,
        run_id: str,
        event_type: str,
        payload: Any = None,
        node_key: str | None = None,
    ) -> dict[str, Any]:
        created_at = now_iso()
        with self._lock, self.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM workflow_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            seq = int(row["next_seq"])
            conn.execute(
                """
                INSERT INTO workflow_events(run_id, seq, event_type, node_key, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, seq, event_type, node_key, encode_json(payload or {}), created_at),
            )
        return {
            "run_id": run_id,
            "seq": seq,
            "event_type": event_type,
            "node_key": node_key,
            "payload": payload or {},
            "created_at": created_at,
        }

    def list_workflow_events(self, run_id: str, limit: int = 500) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM workflow_events WHERE run_id = ? ORDER BY seq ASC LIMIT ?",
                (run_id, limit),
            ).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def create_workflow_trigger(self, payload: dict[str, Any]) -> dict[str, Any]:
        trigger_id = payload.get("id") or new_id("wtr")
        now = now_iso()
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO workflow_triggers(
                  id, workflow_definition_id, name, type, config, enabled,
                  last_fired_at, next_fire_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trigger_id,
                    payload["workflow_definition_id"],
                    payload.get("name") or "Manual trigger",
                    payload.get("type") or "manual",
                    encode_json(payload.get("config") or {}),
                    1 if payload.get("enabled", True) else 0,
                    payload.get("last_fired_at"),
                    payload.get("next_fire_at"),
                    now,
                    now,
                ),
            )
        self.audit("workflow_trigger.create", "workflow_trigger", trigger_id, {"workflow_definition_id": payload["workflow_definition_id"]})
        return self.get_workflow_trigger(trigger_id) or {}

    def get_workflow_trigger(self, trigger_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM workflow_triggers WHERE id = ?", (trigger_id,)).fetchone()
        return row_to_dict(row)

    def update_workflow_trigger(self, trigger_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {
            "name": "name",
            "type": "type",
            "config": "config",
            "enabled": "enabled",
            "last_fired_at": "last_fired_at",
            "next_fire_at": "next_fire_at",
        }
        fields: dict[str, Any] = {}
        for key, column in allowed.items():
            if key in payload:
                value = payload[key]
                if key == "config":
                    value = encode_json(value or {})
                elif key == "enabled":
                    value = 1 if value else 0
                fields[column] = value
        if not fields:
            return self.get_workflow_trigger(trigger_id)
        fields["updated_at"] = now_iso()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._lock, self.connect() as conn:
            cursor = conn.execute(f"UPDATE workflow_triggers SET {assignments} WHERE id = ?", list(fields.values()) + [trigger_id])
        if cursor.rowcount:
            self.audit("workflow_trigger.update", "workflow_trigger", trigger_id)
        return self.get_workflow_trigger(trigger_id)

    def delete_workflow_trigger(self, trigger_id: str) -> bool:
        with self._lock, self.connect() as conn:
            cursor = conn.execute("DELETE FROM workflow_triggers WHERE id = ?", (trigger_id,))
        deleted = cursor.rowcount > 0
        if deleted:
            self.audit("workflow_trigger.delete", "workflow_trigger", trigger_id)
        return deleted

    def list_workflow_triggers(
        self,
        limit: int = 100,
        workflow_definition_id: str | None = None,
        enabled: bool | None = None,
    ) -> list[dict[str, Any]]:
        where = []
        params: list[Any] = []
        if workflow_definition_id:
            where.append("workflow_definition_id = ?")
            params.append(workflow_definition_id)
        if enabled is not None:
            where.append("enabled = ?")
            params.append(1 if enabled else 0)
        sql = "SELECT * FROM workflow_triggers"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def append_workflow_trigger_event(
        self,
        trigger_id: str,
        state: str,
        payload: Any = None,
        run_id: str | None = None,
        error: str | None = None,
        dedupe_key: str | None = None,
    ) -> dict[str, Any]:
        event_id = new_id("wte")
        created_at = now_iso()
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO workflow_trigger_events(id, trigger_id, run_id, payload, state, error, dedupe_key, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (event_id, trigger_id, run_id, encode_json(payload or {}), state, error, dedupe_key, created_at),
            )
        return {
            "id": event_id,
            "trigger_id": trigger_id,
            "run_id": run_id,
            "payload": payload or {},
            "state": state,
            "error": error,
            "dedupe_key": dedupe_key,
            "created_at": created_at,
        }

    def list_workflow_trigger_events(self, trigger_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM workflow_trigger_events
                WHERE trigger_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (trigger_id, limit),
            ).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def create_artifact(self, payload: dict[str, Any]) -> dict[str, Any]:
        artifact_id = payload.get("id") or new_id("art")
        now = now_iso()
        content = payload.get("content", "")
        if not isinstance(content, str):
            content = encode_json(content)
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO artifacts(id, run_id, job_id, key, kind, content, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    payload.get("run_id"),
                    payload.get("job_id"),
                    payload["key"],
                    payload.get("kind") or "text",
                    content,
                    encode_json(payload.get("metadata") or {}),
                    now,
                    now,
                ),
            )
        self.audit("artifact.create", "artifact", artifact_id, {"run_id": payload.get("run_id"), "key": payload["key"]})
        return self.get_artifact(artifact_id) or {}

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        return row_to_dict(row)

    def list_artifacts(
        self,
        limit: int = 100,
        run_id: str | None = None,
        job_id: str | None = None,
        key: str | None = None,
    ) -> list[dict[str, Any]]:
        where = []
        params: list[Any] = []
        if run_id:
            where.append("run_id = ?")
            params.append(run_id)
        if job_id:
            where.append("job_id = ?")
            params.append(job_id)
        if key:
            where.append("key = ?")
            params.append(key)
        sql = "SELECT * FROM artifacts"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
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
