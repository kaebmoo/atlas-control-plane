from __future__ import annotations

import base64
import contextvars
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import uuid
import warnings
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .auth import generate_api_token, hash_api_token, hash_password, verify_password


ARTIFACT_KINDS = frozenset({"text", "json", "markdown", "file_ref", "summary", "decision"})
ROLES = frozenset({"admin", "operator", "viewer", "auditor"})
USER_STATUSES = frozenset({"active", "disabled"})
_WORKER_TOKEN_MARKER = "atlasenc:v1:"
_AUDIT_ACTOR = contextvars.ContextVar("atlas_audit_actor", default="local")


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def atomic_write_0600(path: Path, data: bytes) -> None:
    """Write bytes to path atomically at 0600: a temp file in the same directory is written,
    fsynced, then os.replace()d over the target. A short write or disk error leaves the
    previous file intact and removes the partial temp — unlike an in-place O_TRUNC, which
    destroys the original before the new bytes are safely on disk. Used for secret files
    (BYOK env, fleet token sidecar)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique per call: PID alone collides across concurrent threads/calls in one process,
    # where one writer's os.replace would yank the temp out from under another (FileNotFoundError)
    # or two writers would interleave on the same temp path.
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)  # tighten even if the temp pre-existed at a looser mode
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    """Write text atomically (unique temp + fsync + os.replace) at default perms. For durable
    artifacts that must never be left truncated by a crash or an in-place rewrite — CDR bills,
    signed usage exports — but that are meant to be read by other users, unlike the 0600
    secret writer above."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


_SAFE_COLUMN_CHARS = set("abcdefghijklmnopqrstuvwxyz_")


def _set_clause(fields: dict[str, Any]) -> str:
    """Build a `col = ?, ...` assignment clause for an UPDATE, asserting every key is a bare
    [a-z_] identifier. Values are always bound with `?`; this constrains the only
    string-interpolated part (the column names) so the clause is injection-safe BY
    CONSTRUCTION — not merely because every current caller happens to pass literal column
    names. A non-identifier key (e.g. attacker-controlled) raises instead of reaching SQL."""
    bad = [key for key in fields if not key or set(key) - _SAFE_COLUMN_CHARS]
    if bad:
        raise ValueError(f"unsafe column name(s) in update: {bad}")
    return ", ".join(f"{key} = ?" for key in fields)


def encode_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def decode_json(value: str | None, fallback: Any = None) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    list_fields = {"tags", "current_nodes", "input_artifacts", "output_artifacts", "choices"}
    json_fields = {
        "agent_info",
        "condition_result",
        "config",
        "counters",
        "details",
        "graph",
        "graph_snapshot",
        "input",
        "metadata",
        "payload",
        "policy",
        "policy_snapshot",
    }
    for key in list_fields | json_fields:
        if key in data:
            data[key] = decode_json(data[key], [] if key in list_fields else {})
    return data


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_tokens (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL DEFAULT '',
  last_used_at TEXT,
  created_at TEXT NOT NULL,
  revoked_at TEXT,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_api_tokens_user ON api_tokens(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_api_tokens_hash ON api_tokens(token_hash);

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

CREATE TABLE IF NOT EXISTS usage_events (
  id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  run_id TEXT,
  job_id TEXT,
  node_key TEXT,
  worker_id TEXT,
  actor TEXT,
  kind TEXT NOT NULL,
  status TEXT,
  units INTEGER NOT NULL DEFAULT 1,
  seconds REAL,
  started_at TEXT,
  finished_at TEXT,
  model TEXT,
  tokens_prompt INTEGER,
  tokens_output INTEGER,
  created_at TEXT NOT NULL,
  metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_usage_events_created ON usage_events(created_at, id);
CREATE INDEX IF NOT EXISTS idx_usage_events_run ON usage_events(run_id);
CREATE INDEX IF NOT EXISTS idx_usage_events_job ON usage_events(job_id);

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

CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  workflow_node_id TEXT,
  node_key TEXT NOT NULL,
  approval_key TEXT NOT NULL,
  label TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  choices TEXT NOT NULL DEFAULT '[]',
  selected_choice TEXT,
  state TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL,
  decided_at TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE(run_id, approval_key),
  FOREIGN KEY(run_id) REFERENCES workflow_runs(id) ON DELETE CASCADE,
  FOREIGN KEY(workflow_node_id) REFERENCES workflow_nodes(id) ON DELETE CASCADE
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
CREATE INDEX IF NOT EXISTS idx_approvals_state_created ON approvals(state, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_approvals_run ON approvals(run_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id);
CREATE INDEX IF NOT EXISTS idx_workflow_triggers_definition ON workflow_triggers(workflow_definition_id);
CREATE INDEX IF NOT EXISTS idx_workflow_trigger_events_trigger ON workflow_trigger_events(trigger_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_workflow_trigger_events_dedupe ON workflow_trigger_events(trigger_id, dedupe_key);

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


def _add_missing_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _migration_002_jobs_columns(conn: sqlite3.Connection) -> None:
    _add_missing_columns(
        conn,
        "jobs",
        {
            "parent_job_id": "TEXT",
            "handoff_worker_id": "TEXT",
            "handoff_workspace_id": "TEXT",
            "handoff_prompt": "TEXT NOT NULL DEFAULT ''",
            "handoff_job_id": "TEXT",
            "handoff_error": "TEXT",
        },
    )


def _migration_003_approval_columns(conn: sqlite3.Connection) -> None:
    _add_missing_columns(
        conn,
        "approvals",
        {
            "choices": "TEXT NOT NULL DEFAULT '[]'",
            "selected_choice": "TEXT",
        },
    )


def _migration_004_workflow_run_snapshot(conn: sqlite3.Connection) -> None:
    # Snapshot the graph/policy a run started with, so resume/recovery executes the SAME
    # definition the run began on even if the live workflow_definition is edited or deleted
    # mid-flight. NULL on rows created before this migration -> callers fall back to the
    # live definition for those legacy runs.
    _add_missing_columns(
        conn,
        "workflow_runs",
        {
            "graph_snapshot": "TEXT",
            "policy_snapshot": "TEXT",
        },
    )


def _migration_005_deliveries(conn: sqlite3.Connection) -> None:
    # OB-1: the outbound-delivery ledger (docs/plans/input-adapter-return-path-plan.md). A NEW
    # table as a numbered step (not folded into SCHEMA) so it is created for databases that
    # already migrated past version 1. No per-tenant column (silo invariant, scripts/check_silo.py).
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS deliveries (
          id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL,
          url TEXT NOT NULL,
          correlation_id TEXT,
          status TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0,
          max_attempts INTEGER NOT NULL DEFAULT 5,
          last_error TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          delivered_at TEXT,
          FOREIGN KEY(run_id) REFERENCES workflow_runs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_deliveries_run ON deliveries(run_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_deliveries_status ON deliveries(status, updated_at);
        """
    )


# Ordered, append-only migration steps. A step is either a SQL string (run via
# executescript) or a callable(conn). The 1-based index is the schema version.
# Every step MUST be idempotent on its own: SCHEMA is all CREATE ... IF NOT EXISTS,
# the column steps guard with PRAGMA table_info. That makes the runner crash-safe
# (a step re-run after a crash-before-version-record is a no-op) and lets a legacy
# pre-schema_version DB migrate forward by simply applying every step.
# ponytail: append new tables/columns as new steps here; never edit a shipped step.
MIGRATIONS: list[str | Any] = [
    SCHEMA,
    _migration_002_jobs_columns,
    _migration_003_approval_columns,
    _migration_004_workflow_run_snapshot,
    _migration_005_deliveries,
]
SCHEMA_VERSION = len(MIGRATIONS)


class Database:
    def __init__(self, path: Path, secret_key: str | None = None):
        self.path = path
        self._lock = threading.RLock()
        self._secret_key = secret_key if secret_key is not None else (os.getenv("ATLAS_SECRET_KEY") or None)
        self._plaintext_warning_emitted = False
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
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            " version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_version").fetchone()["v"]
        for version, step in enumerate(MIGRATIONS, start=1):
            if version <= applied:
                continue
            if callable(step):
                step(conn)
            else:
                conn.executescript(step)
            conn.execute(
                "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                (version, now_iso()),
            )

    def schema_version(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_version").fetchone()
        return int(row["v"])

    @contextmanager
    def as_actor(self, actor: str) -> Iterator[None]:
        token = _AUDIT_ACTOR.set(actor or "local")
        try:
            yield
        finally:
            _AUDIT_ACTOR.reset(token)

    def audit(self, action: str, resource_type: str, resource_id: str, details: Any = None, actor: str | None = None) -> None:
        actor = actor or _AUDIT_ACTOR.get()
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

    def emit_usage_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        idempotency_key = str(payload.get("idempotency_key") or "").strip()
        kind = str(payload.get("kind") or "").strip()
        if not idempotency_key:
            raise ValueError("usage event idempotency_key is required")
        if not kind:
            raise ValueError("usage event kind is required")
        event_id = str(payload.get("id") or new_id("usg"))
        created_at = str(payload.get("created_at") or now_iso())
        actor = str(payload.get("actor") or _AUDIT_ACTOR.get())
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO usage_events(
                  id, idempotency_key, run_id, job_id, node_key, worker_id,
                  actor, kind, status, units, seconds, started_at, finished_at,
                  model, tokens_prompt, tokens_output, created_at, metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    idempotency_key,
                    payload.get("run_id"),
                    payload.get("job_id"),
                    payload.get("node_key"),
                    payload.get("worker_id"),
                    actor,
                    kind,
                    payload.get("status"),
                    max(0, int(payload.get("units", 1) or 0)),  # never let a negative units deflate the billed total
                    payload.get("seconds"),
                    payload.get("started_at"),
                    payload.get("finished_at"),
                    payload.get("model"),
                    payload.get("tokens_prompt"),
                    payload.get("tokens_output"),
                    created_at,
                    encode_json(payload.get("metadata") or {}),
                ),
            )
            row = conn.execute(
                "SELECT * FROM usage_events WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return row_to_dict(row) or {}

    def list_usage_events(self, from_at: str | None = None, to_at: str | None = None) -> list[dict[str, Any]]:
        where = []
        params: list[Any] = []
        if from_at:
            where.append("julianday(created_at) >= julianday(?)")
            params.append(from_at)
        if to_at:
            where.append("julianday(created_at) <= julianday(?)")
            params.append(to_at)
        sql = "SELECT * FROM usage_events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at ASC, id ASC"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def workflow_context_for_job(self, job_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT workflow_nodes.run_id, workflow_nodes.node_key
                FROM workflow_nodes
                WHERE workflow_nodes.job_id = ?
                ORDER BY workflow_nodes.created_at DESC
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
        return row_to_dict(row) or {}

    def create_user(self, username: str, password: str, role: str = "viewer", status: str = "active") -> dict[str, Any]:
        username = str(username or "").strip()
        if not username:
            raise ValueError("username is required")
        self._validate_role_status(role, status)
        user_id = new_id("usr")
        now = now_iso()
        try:
            with self._lock, self.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO users(id, username, password_hash, role, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (user_id, username, hash_password(password), role, status, now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"username already exists: {username}") from exc
        self.audit("user.create", "user", user_id, {"username": username, "role": role, "status": status})
        return self.get_user(user_id) or {}

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, username, role, status, created_at, updated_at FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        return row_to_dict(row)

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, username, role, status, created_at, updated_at FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        return row_to_dict(row)

    def list_users(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT users.id, users.username, users.role, users.status, users.created_at, users.updated_at,
                  COUNT(api_tokens.id) AS token_count
                FROM users LEFT JOIN api_tokens ON api_tokens.user_id = users.id AND api_tokens.revoked_at IS NULL
                GROUP BY users.id ORDER BY users.username COLLATE NOCASE
                """
            ).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def update_user(self, user_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        user = self.get_user(user_id)
        if not user:
            return None
        fields: dict[str, Any] = {}
        if "username" in payload:
            username = str(payload["username"] or "").strip()
            if not username:
                raise ValueError("username is required")
            fields["username"] = username
        role = str(payload.get("role", user["role"]))
        status = str(payload.get("status", user["status"]))
        self._validate_role_status(role, status)
        if "role" in payload:
            fields["role"] = role
        if "status" in payload:
            fields["status"] = status
        if "password" in payload:
            fields["password_hash"] = hash_password(str(payload["password"] or ""))
        if not fields:
            return user
        fields["updated_at"] = now_iso()
        assignments = _set_clause(fields)
        try:
            with self._lock, self.connect() as conn:
                conn.execute(f"UPDATE users SET {assignments} WHERE id = ?", [*fields.values(), user_id])  # nosec B608
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"username already exists: {fields.get('username')}") from exc
        self.audit("user.update", "user", user_id, {key: value for key, value in fields.items() if key != "password_hash"})
        return self.get_user(user_id)

    def delete_user(self, user_id: str) -> bool:
        with self._lock, self.connect() as conn:
            cursor = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        deleted = cursor.rowcount > 0
        if deleted:
            self.audit("user.delete", "user", user_id)
        return deleted

    def verify_user_password(self, username: str, password: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if not row or row["status"] != "active" or not verify_password(password, row["password_hash"]):
            return None
        return {key: row[key] for key in ("id", "username", "role", "status", "created_at", "updated_at")}

    def create_api_token(self, user_id: str, name: str = "") -> tuple[dict[str, Any], str]:
        user = self.get_user(user_id)
        if not user:
            raise ValueError(f"Unknown user_id: {user_id}")
        raw_token = generate_api_token()
        token_id = new_id("tok")
        created_at = now_iso()
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO api_tokens(id, user_id, token_hash, name, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (token_id, user_id, hash_api_token(raw_token), str(name or ""), created_at),
            )
        self.audit("api_token.create", "api_token", token_id, {"user_id": user_id, "name": str(name or "")})
        return self.get_api_token(token_id) or {}, raw_token

    def get_api_token(self, token_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT api_tokens.id, api_tokens.user_id, api_tokens.name, api_tokens.last_used_at,
                  api_tokens.created_at, api_tokens.revoked_at, users.username
                FROM api_tokens JOIN users ON users.id = api_tokens.user_id
                WHERE api_tokens.id = ?
                """,
                (token_id,),
            ).fetchone()
        return row_to_dict(row)

    def list_api_tokens(self, user_id: str | None = None) -> list[dict[str, Any]]:
        sql = """
            SELECT api_tokens.id, api_tokens.user_id, api_tokens.name, api_tokens.last_used_at,
              api_tokens.created_at, api_tokens.revoked_at, users.username
            FROM api_tokens JOIN users ON users.id = api_tokens.user_id
        """
        params: tuple[Any, ...] = ()
        if user_id:
            sql += " WHERE api_tokens.user_id = ?"
            params = (user_id,)
        sql += " ORDER BY api_tokens.created_at DESC"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def authenticate_api_token(self, raw_token: str) -> dict[str, Any] | None:
        candidate = hash_api_token(raw_token)
        with self._lock, self.connect() as conn:
            row = conn.execute(
                """
                SELECT api_tokens.id AS token_id, api_tokens.token_hash, users.id, users.username,
                  users.role, users.status, users.created_at, users.updated_at
                FROM api_tokens JOIN users ON users.id = api_tokens.user_id
                WHERE api_tokens.token_hash = ? AND api_tokens.revoked_at IS NULL
                """,
                (candidate,),
            ).fetchone()
            if not row or row["status"] != "active" or not hmac.compare_digest(candidate, row["token_hash"]):
                return None
            conn.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (now_iso(), row["token_id"]))
        return {
            "id": row["id"],
            "username": row["username"],
            "role": row["role"],
            "status": row["status"],
            "token_id": row["token_id"],
        }

    def revoke_api_token(self, token_id: str) -> bool:
        with self._lock, self.connect() as conn:
            cursor = conn.execute(
                "UPDATE api_tokens SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ? AND revoked_at IS NULL",
                (now_iso(), token_id),
            )
        revoked = cursor.rowcount > 0
        if revoked:
            self.audit("api_token.revoke", "api_token", token_id)
        return revoked

    def update_api_token(self, token_id: str, name: str) -> dict[str, Any] | None:
        with self._lock, self.connect() as conn:
            cursor = conn.execute("UPDATE api_tokens SET name = ? WHERE id = ?", (str(name or ""), token_id))
        if not cursor.rowcount:
            return None
        self.audit("api_token.update", "api_token", token_id, {"name": str(name or "")})
        return self.get_api_token(token_id)

    @staticmethod
    def _validate_role_status(role: str, status: str) -> None:
        if role not in ROLES:
            raise ValueError(f"role must be one of: {', '.join(sorted(ROLES))}")
        if status not in USER_STATUSES:
            raise ValueError(f"status must be one of: {', '.join(sorted(USER_STATUSES))}")

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
        assignments = _set_clause(fields)
        with self._lock, self.connect() as conn:
            cursor = conn.execute(f"UPDATE workflow_definitions SET {assignments} WHERE id = ?", list(fields.values()) + [definition_id])  # nosec B608
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
                  counters, error, created_at, started_at, finished_at, updated_at,
                  graph_snapshot, policy_snapshot
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    encode_json(payload["graph_snapshot"]) if payload.get("graph_snapshot") is not None else None,
                    encode_json(payload["policy_snapshot"]) if payload.get("policy_snapshot") is not None else None,
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
        assignments = _set_clause(fields)
        with self._lock, self.connect() as conn:
            conn.execute(f"UPDATE workflow_runs SET {assignments} WHERE id = ?", list(fields.values()) + [run_id])  # nosec B608

    def finalize_workflow_run(self, run_id: str, state: str, allowed_from: tuple[str, ...] | None = None, **fields: Any) -> bool:
        """Atomically transition a run's state via a single check-and-set UPDATE. Returns True
        iff THIS call performed the transition.

        - Default (allowed_from=None): permit from any NON-terminal state. cancel_run uses this,
          so a cancel can override paused / waiting_for_human / recovery_required.
        - allowed_from=(...): permit ONLY from those exact states. The runner's success/failure
          finish passes ('running',), so a runner draining to empty can NEVER overwrite a run
          another path just moved to paused / waiting_for_human / recovery_required (nor a
          concurrent cancel), and can't double-emit run_finished / usage."""
        for key in ("current_nodes", "counters"):
            if key in fields:
                fields[key] = encode_json(fields[key])
        fields = {"state": state, **fields, "updated_at": now_iso()}
        assignments = _set_clause(fields)
        if allowed_from is None:
            predicate, extra = "state NOT IN ('succeeded', 'failed', 'cancelled')", []
        else:
            predicate, extra = f"state IN ({', '.join('?' for _ in allowed_from)})", list(allowed_from)
        with self._lock, self.connect() as conn:
            cursor = conn.execute(
                f"UPDATE workflow_runs SET {assignments} WHERE id = ? AND {predicate}",  # nosec B608
                list(fields.values()) + [run_id] + extra,
            )
        return cursor.rowcount > 0

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
        assignments = _set_clause(fields)
        with self._lock, self.connect() as conn:
            conn.execute(f"UPDATE workflow_nodes SET {assignments} WHERE id = ?", list(fields.values()) + [node_id])  # nosec B608

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

    def create_approval(self, payload: dict[str, Any]) -> dict[str, Any]:
        approval_id = payload.get("id") or new_id("apr")
        now = now_iso()
        with self._lock, self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO approvals(
                  id, run_id, workflow_node_id, node_key, approval_key, label,
                  reason, choices, state, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    approval_id,
                    payload["run_id"],
                    payload.get("workflow_node_id"),
                    payload["node_key"],
                    payload["approval_key"],
                    payload.get("label") or "Human approval required",
                    payload.get("reason") or "",
                    encode_json(payload.get("choices") or []),
                    now,
                    now,
                ),
            )
            if not cursor.rowcount:
                row = conn.execute(
                    "SELECT * FROM approvals WHERE run_id = ? AND approval_key = ?",
                    (payload["run_id"], payload["approval_key"]),
                ).fetchone()
                return row_to_dict(row) or {}
        self.audit("approval.create", "approval", approval_id, {"run_id": payload["run_id"], "node_key": payload["node_key"]})
        return self.get_approval(approval_id) or {}

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return row_to_dict(row)

    def list_approvals(
        self,
        limit: int = 100,
        state: str | None = None,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        where = []
        params: list[Any] = []
        if state:
            where.append("state = ?")
            params.append(state)
        if run_id:
            where.append("run_id = ?")
            params.append(run_id)
        sql = "SELECT * FROM approvals"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def decide_approval(self, approval_id: str, decision: str) -> dict[str, Any]:
        if decision not in {"approved", "rejected"}:
            raise ValueError(f"unsupported approval decision: {decision}")
        now = now_iso()
        with self._lock, self.connect() as conn:
            approval = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            if not approval:
                raise ValueError(f"Unknown approval_id: {approval_id}")
            if approval["state"] != "pending":
                raise ValueError(f"approval {approval_id} already {approval['state']}")
            conn.execute(
                "UPDATE approvals SET state = ?, decided_at = ?, updated_at = ? WHERE id = ? AND state = 'pending'",
                (decision, now, now, approval_id),
            )
        self.audit(f"approval.{decision}", "approval", approval_id, {"run_id": approval["run_id"], "node_key": approval["node_key"]})
        return self.get_approval(approval_id) or {}

    def choose_approval(self, approval_id: str, choice: str) -> dict[str, Any]:
        now = now_iso()
        with self._lock, self.connect() as conn:
            approval = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            if not approval:
                raise ValueError(f"Unknown approval_id: {approval_id}")
            if approval["state"] != "pending":
                raise ValueError(f"approval {approval_id} already {approval['state']}")
            choices = decode_json(approval["choices"], [])
            if choice not in {item.get("id") for item in choices if isinstance(item, dict)}:
                raise ValueError(f"unknown approval choice: {choice}")
            cursor = conn.execute(
                "UPDATE approvals SET state = 'chosen', selected_choice = ?, decided_at = ?, updated_at = ? WHERE id = ? AND state = 'pending'",
                (choice, now, now, approval_id),
            )
            if not cursor.rowcount:
                raise ValueError(f"approval {approval_id} was already decided")
        self.audit("approval.chosen", "approval", approval_id, {"run_id": approval["run_id"], "node_key": approval["node_key"], "choice": choice})
        return self.get_approval(approval_id) or {}

    def cancel_pending_approvals(self, run_id: str) -> int:
        now = now_iso()
        with self._lock, self.connect() as conn:
            cursor = conn.execute(
                "UPDATE approvals SET state = 'cancelled', decided_at = ?, updated_at = ? WHERE run_id = ? AND state = 'pending'",
                (now, now, run_id),
            )
        return cursor.rowcount

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
        assignments = _set_clause(fields)
        with self._lock, self.connect() as conn:
            cursor = conn.execute(f"UPDATE workflow_triggers SET {assignments} WHERE id = ?", list(fields.values()) + [trigger_id])  # nosec B608
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
        sql = """
            SELECT workflow_triggers.*,
              (SELECT state FROM workflow_trigger_events WHERE trigger_id = workflow_triggers.id ORDER BY rowid DESC LIMIT 1) AS last_event_state,
              (SELECT error FROM workflow_trigger_events WHERE trigger_id = workflow_triggers.id ORDER BY rowid DESC LIMIT 1) AS last_event_error,
              (SELECT created_at FROM workflow_trigger_events WHERE trigger_id = workflow_triggers.id ORDER BY rowid DESC LIMIT 1) AS last_event_at
            FROM workflow_triggers
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def has_workflow_trigger_event_dedupe(self, trigger_id: str, dedupe_key: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM workflow_trigger_events WHERE trigger_id = ? AND dedupe_key = ? LIMIT 1",
                (trigger_id, dedupe_key),
            ).fetchone()
        return row is not None

    def claim_trigger_dedupe(self, trigger_id: str, dedupe_key: str, payload: Any = None) -> bool:
        """Atomically claim a dedupe_key by inserting the 'received' event only if no event
        with that key exists yet. Returns True if claimed (caller starts the run), False if it
        was already claimed (duplicate). self._lock serializes the check-and-insert against
        concurrent fires in-process, closing the TOCTOU window that let two requests with the
        same dedupe_key both start a run. (Atlas runs one process per instance.)"""
        with self._lock, self.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM workflow_trigger_events WHERE trigger_id = ? AND dedupe_key = ? LIMIT 1",
                (trigger_id, dedupe_key),
            ).fetchone()
            if exists:
                return False
            conn.execute(
                "INSERT INTO workflow_trigger_events(id, trigger_id, run_id, payload, state, error, dedupe_key, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (new_id("wte"), trigger_id, None, encode_json(payload or {}), "received", None, dedupe_key, now_iso()),
            )
        return True

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
                ORDER BY rowid DESC
                LIMIT ?
                """,
                (trigger_id, limit),
            ).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def create_artifact(self, payload: dict[str, Any]) -> dict[str, Any]:
        artifact_id = payload.get("id") or new_id("art")
        now = now_iso()
        key = payload.get("key")
        if not isinstance(key, str) or not key.strip():
            raise ValueError("artifact key is required")
        kind = payload.get("kind", "text")
        if not isinstance(kind, str) or kind not in ARTIFACT_KINDS:
            raise ValueError(f"unsupported artifact kind: {kind}")
        content = payload.get("content", "")
        if kind == "json":
            try:
                content = encode_json(json.loads(content) if isinstance(content, str) else content)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise ValueError("json artifact content must be valid JSON") from exc
        elif not isinstance(content, str):
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
                    key,
                    kind,
                    content,
                    encode_json(payload.get("metadata") or {}),
                    now,
                    now,
                ),
            )
        self.audit("artifact.create", "artifact", artifact_id, {"run_id": payload.get("run_id"), "key": key})
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
        # rowid tiebreaker: created_at is second-resolution, so two artifacts written to the
        # same key in the same second would otherwise have undefined order, and last-write-wins
        # in _load_artifacts could pick the older value. Insertion order (rowid) breaks the tie.
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def create_delivery(self, payload: dict[str, Any]) -> dict[str, Any]:
        delivery_id = payload.get("id") or new_id("dlv")
        now = now_iso()
        status = payload.get("status") or "pending"
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO deliveries(
                  id, run_id, url, correlation_id, status, attempts, max_attempts,
                  last_error, created_at, updated_at, delivered_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_id,
                    payload["run_id"],
                    payload["url"],
                    payload.get("correlation_id"),
                    status,
                    int(payload.get("attempts", 0) or 0),
                    int(payload.get("max_attempts", 5) or 5),
                    payload.get("last_error"),
                    now,
                    now,
                    payload.get("delivered_at"),
                ),
            )
        self.audit("delivery.create", "delivery", delivery_id, {"run_id": payload["run_id"], "status": status})
        return self.get_delivery(delivery_id) or {}

    def get_delivery(self, delivery_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
        return row_to_dict(row)

    def list_deliveries(self, limit: int = 100, run_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        where = []
        params: list[Any] = []
        if run_id:
            where.append("run_id = ?")
            params.append(run_id)
        if status:
            where.append("status = ?")
            params.append(status)
        sql = "SELECT * FROM deliveries"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [row_to_dict(row) or {} for row in rows]

    def update_delivery(self, delivery_id: str, **fields: Any) -> dict[str, Any] | None:
        if not fields:
            return self.get_delivery(delivery_id)
        fields["updated_at"] = now_iso()
        assignments = _set_clause(fields)
        with self._lock, self.connect() as conn:
            conn.execute(f"UPDATE deliveries SET {assignments} WHERE id = ?", list(fields.values()) + [delivery_id])  # nosec B608
        return self.get_delivery(delivery_id)

    def upsert_worker(self, payload: dict[str, Any]) -> dict[str, Any]:
        worker_id = payload.get("id") or new_id("wrk")
        now = now_iso()
        tags = payload.get("tags") or []
        if isinstance(tags, str):
            tags = [tag.strip() for tag in tags.split(",") if tag.strip()]
        if not payload.get("base_url"):
            raise ValueError("base_url is required")
        base_url = str(payload["base_url"]).rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            # Only http(s): a file:// or custom-scheme base_url would make the worker
            # health/agent urlopen read a local file or hit an unexpected scheme (SSRF/LFI).
            raise ValueError("worker base_url must be an http(s) URL")
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
            token = self._encrypt_worker_token(token)
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
            self._reencrypt_worker_token(conn, worker_id)
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
        worker = row_to_dict(row)
        if worker:
            worker["token"] = self._decrypt_worker_token(worker.get("token"))
        return worker

    def list_workers(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM workers ORDER BY name COLLATE NOCASE").fetchall()
        workers = [row_to_dict(row) or {} for row in rows]
        for worker in workers:
            worker["token"] = self._decrypt_worker_token(worker.get("token"))
        return workers

    def _encrypt_worker_token(self, token: Any) -> str | None:
        if token is None or token == "":
            return None
        token = str(token)
        if token.startswith(_WORKER_TOKEN_MARKER):
            return token
        if not self._secret_key:
            self._warn_plaintext_worker_tokens()
            return token
        nonce = secrets.token_bytes(16)
        plaintext = token.encode("utf-8")
        encryption_key, mac_key = self._worker_token_keys()
        ciphertext = bytes(value ^ mask for value, mask in zip(plaintext, self._keystream(encryption_key, nonce, len(plaintext)), strict=True))
        tag = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
        encoded = base64.urlsafe_b64encode(nonce + ciphertext + tag).decode("ascii")
        return _WORKER_TOKEN_MARKER + encoded

    def _decrypt_worker_token(self, token: Any) -> str | None:
        if token is None or token == "":
            return None
        token = str(token)
        if not token.startswith(_WORKER_TOKEN_MARKER):
            if not self._secret_key:
                self._warn_plaintext_worker_tokens()
            return token
        if not self._secret_key:
            raise ValueError("ATLAS_SECRET_KEY is required to decrypt stored worker tokens")
        try:
            packed = base64.urlsafe_b64decode(token.removeprefix(_WORKER_TOKEN_MARKER).encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise ValueError("stored worker token ciphertext is invalid") from exc
        if len(packed) < 48:
            raise ValueError("stored worker token ciphertext is invalid")
        nonce, ciphertext, expected_tag = packed[:16], packed[16:-32], packed[-32:]
        encryption_key, mac_key = self._worker_token_keys()
        actual_tag = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(actual_tag, expected_tag):
            raise ValueError("stored worker token could not be authenticated; check ATLAS_SECRET_KEY")
        plaintext = bytes(value ^ mask for value, mask in zip(ciphertext, self._keystream(encryption_key, nonce, len(ciphertext)), strict=True))
        return plaintext.decode("utf-8")

    def _worker_token_keys(self) -> tuple[bytes, bytes]:
        master = str(self._secret_key).encode("utf-8")
        return (
            hmac.new(master, b"atlas-worker-token-encryption-v1", hashlib.sha256).digest(),
            hmac.new(master, b"atlas-worker-token-authentication-v1", hashlib.sha256).digest(),
        )

    @staticmethod
    def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
        output = bytearray()
        counter = 0
        while len(output) < length:
            output.extend(hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest())
            counter += 1
        return bytes(output[:length])

    def _reencrypt_worker_token(self, conn: sqlite3.Connection, worker_id: str) -> None:
        if not self._secret_key:
            return
        row = conn.execute("SELECT token FROM workers WHERE id = ?", (worker_id,)).fetchone()
        if row and row["token"] and not row["token"].startswith(_WORKER_TOKEN_MARKER):
            conn.execute("UPDATE workers SET token = ? WHERE id = ?", (self._encrypt_worker_token(row["token"]), worker_id))

    def _warn_plaintext_worker_tokens(self) -> None:
        if self._plaintext_warning_emitted:
            return
        self._plaintext_warning_emitted = True
        warnings.warn(
            "ATLAS_SECRET_KEY is unset; worker tokens will remain plaintext at rest",
            RuntimeWarning,
            stacklevel=3,
        )

    def delete_worker(self, worker_id: str) -> bool:
        with self._lock, self.connect() as conn:
            # The jobs FK is ON DELETE CASCADE, so deleting a worker would silently destroy
            # its jobs and job_events. Job history is an audit record — block the delete when
            # any job references the worker rather than cascade it away.
            job_count = conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE worker_id = ?", (worker_id,)).fetchone()["n"]
            if job_count:
                raise ValueError(f"worker has {job_count} job(s) in history; deletion is blocked to preserve the audit trail")
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
        for required in ("worker_id", "workspace_key", "workspace_dir"):
            if not payload.get(required):
                raise ValueError(f"{required} is required")
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
                ORDER BY updated_at DESC, rowid DESC
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
        return row_to_dict(row)

    def upsert_session_binding(self, conversation_id: str, worker_id: str, workspace_id: str | None, thclaws_session_id: str) -> None:
        now = now_iso()
        with self._lock, self.connect() as conn:
            if workspace_id is None:
                # SQLite treats NULL as distinct in a UNIQUE index, so the ON CONFLICT
                # upsert never matches a workspace-less binding. Update it by hand so
                # repeated runs reuse the row instead of piling up duplicates (which would
                # make find_session_binding's "newest wins" lookup ambiguous).
                updated = conn.execute(
                    "UPDATE session_bindings SET thclaws_session_id = ?, updated_at = ?"
                    " WHERE conversation_id = ? AND worker_id = ? AND workspace_id IS NULL",
                    (thclaws_session_id, now, conversation_id, worker_id),
                ).rowcount
                if not updated:
                    conn.execute(
                        "INSERT INTO session_bindings(id, conversation_id, worker_id, workspace_id, thclaws_session_id, created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (new_id("ses"), conversation_id, worker_id, None, thclaws_session_id, now, now),
                    )
            else:
                conn.execute(
                    """
                    INSERT INTO session_bindings(id, conversation_id, worker_id, workspace_id, thclaws_session_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(conversation_id, worker_id, workspace_id) DO UPDATE SET
                      thclaws_session_id=excluded.thclaws_session_id,
                      updated_at=excluded.updated_at
                    """,
                    (new_id("ses"), conversation_id, worker_id, workspace_id, thclaws_session_id, now, now),
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
        assignments = _set_clause(fields)
        values = list(fields.values()) + [job_id]
        with self._lock, self.connect() as conn:
            conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", values)  # nosec B608

    def mark_cancel_requested(self, job_id: str) -> bool:
        """Request cancellation atomically, but ONLY if the job is not already terminal —
        a completion landing between a caller's state read and this write must not regress a
        succeeded/failed job back to 'cancel_requested'. Returns True iff the flag was set."""
        now = now_iso()
        with self._lock, self.connect() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET cancel_requested = 1, state = 'cancel_requested', updated_at = ? "
                "WHERE id = ? AND state NOT IN ('succeeded', 'failed', 'cancelled')",
                (now, job_id),
            )
        if cursor.rowcount:
            self.audit("job.cancel_requested", "job", job_id)
        return cursor.rowcount > 0

    def try_start_job(self, job_id: str) -> bool:
        """Atomically claim a queued job into 'running', but ONLY if it is still queued and no
        cancel has been requested. Returns True iff this call started it. The single
        check-and-set UPDATE closes the TOCTOU window where a cancel landing between a plain
        is_cancel_requested() check and the state write would still open the worker stream."""
        now = now_iso()
        with self._lock, self.connect() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET state = 'running', started_at = ?, updated_at = ? "
                "WHERE id = ? AND state = 'queued' AND cancel_requested = 0",
                (now, now, job_id),
            )
        return cursor.rowcount > 0

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
