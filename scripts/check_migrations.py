from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.db import SCHEMA_VERSION, Database


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def main() -> None:
    with TemporaryDirectory() as tmp:
        # 1. Fresh DB initializes to the final schema_version with all columns.
        fresh = Path(tmp) / "fresh.sqlite"
        db = Database(fresh)
        assert db.schema_version() == SCHEMA_VERSION, db.schema_version()
        with db.connect() as conn:
            assert "handoff_prompt" in _columns(conn, "jobs")
            assert {"choices", "selected_choice"} <= _columns(conn, "approvals")
            rows = conn.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
        assert [r[0] for r in rows] == list(range(1, SCHEMA_VERSION + 1)), rows

        # 2. init() run again is a no-op: version unchanged, no duplicate rows/columns.
        db.init()
        db.init()
        assert db.schema_version() == SCHEMA_VERSION
        with db.connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
            jobs_cols = list(conn.execute("PRAGMA table_info(jobs)").fetchall())
        assert count == SCHEMA_VERSION, count
        assert len({c[1] for c in jobs_cols}) == len(jobs_cols), "duplicate columns after re-init"

        # 3. An older snapshot (no schema_version, missing late columns) migrates forward.
        legacy = Path(tmp) / "legacy.sqlite"
        raw = sqlite3.connect(legacy)
        raw.executescript(
            """
            CREATE TABLE jobs (
              id TEXT PRIMARY KEY, worker_id TEXT NOT NULL, state TEXT NOT NULL,
              prompt TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE approvals (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL, node_key TEXT NOT NULL,
              approval_key TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              UNIQUE(run_id, approval_key)
            );
            INSERT INTO jobs(id, worker_id, state, prompt, created_at, updated_at)
              VALUES ('j1', 'w1', 'done', 'hi', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');
            """
        )
        raw.commit()
        raw.close()
        with sqlite3.connect(legacy) as conn:
            assert "handoff_prompt" not in _columns(conn, "jobs")
            assert "choices" not in _columns(conn, "approvals")
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "schema_version" not in tables

        migrated = Database(legacy)
        assert migrated.schema_version() == SCHEMA_VERSION, migrated.schema_version()
        with migrated.connect() as conn:
            assert "handoff_prompt" in _columns(conn, "jobs")
            assert {"choices", "selected_choice"} <= _columns(conn, "approvals")
            # pre-existing data survived; new tables from the base schema now exist
            assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
            assert "workflow_runs" in {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }

    print("migrations check ok")


if __name__ == "__main__":
    main()
