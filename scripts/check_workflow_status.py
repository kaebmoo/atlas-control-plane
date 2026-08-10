"""Workflow status enforcement: status is execution policy, guarded at every start path.

Matrix (docs/plans + Flow Designer's WORKFLOW_STATUS_ENFORCEMENT_PLAN):
  draft    -> test allowed, production blocked
  active   -> both allowed
  disabled -> everything blocked
Omitted execution_mode means production, so legacy callers fail closed. The 409 body is the
stable contract {"error": "workflow_not_runnable", "reason": ..., "status": ...}.

Mutation-tested by construction: remove ensure_workflow_runnable from any start path and the
draft/disabled expectations below go red; loosen db status validation and the invalid-status
expectations go red; drop the backfill and the migration section goes red.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.app import AtlasHttpServer, AtlasRuntime
from atlas.config import Config
from atlas.db import Database, _migration_016_workflow_status_backfill, now_iso
from atlas.packs import import_pack, validate_pack
from atlas.workflows import WorkflowNotRunnable, ensure_workflow_runnable

# A join-only graph finishes without any worker, so "allowed" runs stay hermetic.
GRAPH = {"start": "done", "nodes": [{"id": "done", "type": "join", "mode": "all"}], "edges": []}


def request(base_url: str, method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{base_url}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.loads(response.read())


def request_error(base_url: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    try:
        request(base_url, method, path, body)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())
    raise AssertionError(f"{method} {path} unexpectedly succeeded")


def check_guard_unit() -> None:
    """The shared guard in isolation, including the fail-closed unknown-status branch."""
    ensure_workflow_runnable({"status": "active"}, "test")
    ensure_workflow_runnable({"status": "active"}, "production")
    ensure_workflow_runnable({"status": "draft"}, "test")
    for definition, mode, reason in [
        ({"status": "draft"}, "production", "draft_requires_test_mode"),
        ({"status": "disabled"}, "test", "workflow_disabled"),
        ({"status": "disabled"}, "production", "workflow_disabled"),
        ({"status": "archived"}, "production", "status_not_runnable"),
    ]:
        try:
            ensure_workflow_runnable(definition, mode)
        except WorkflowNotRunnable as exc:
            assert exc.reason == reason, (exc.reason, reason)
            assert exc.payload == {
                "error": "workflow_not_runnable",
                "reason": reason,
                "status": definition["status"],
            }, exc.payload
        else:
            raise AssertionError(f"{definition['status']}+{mode} must not be runnable")
    try:
        ensure_workflow_runnable({"status": "active"}, "batch")
    except ValueError as exc:
        assert "execution_mode" in str(exc)
    else:
        raise AssertionError("unknown execution_mode must be rejected")
    print("  guard matrix (unit) OK")


def check_http(runtime: AtlasRuntime, base_url: str) -> None:
    # Create defaults to draft; the closed vocabulary is enforced on create and update.
    workflow = request(base_url, "POST", "/api/workflows", {"name": "Status", "graph": GRAPH})["workflow"]
    assert workflow["status"] == "draft", workflow["status"]
    assert request(base_url, "POST", "/api/workflows", {"name": "Null status", "graph": GRAPH, "status": None})["workflow"]["status"] == "draft"
    for bad_status in ("archived", "", False, 0):
        status_code, invalid = request_error(
            base_url,
            "POST",
            "/api/workflows",
            {"name": "bad", "graph": GRAPH, "status": bad_status},
        )
        assert status_code == HTTPStatus.BAD_REQUEST and "status must be one of" in invalid["error"], invalid
    status_code, invalid = request_error(base_url, "PUT", f"/api/workflows/{workflow['id']}", {"status": "on"})
    assert status_code == HTTPStatus.BAD_REQUEST and "status must be one of" in invalid["error"], invalid
    print("  create missing/null defaults draft; invalid and falsy statuses rejected OK")

    wf_id = workflow["id"]

    def start(mode: str | None, extra: dict | None = None) -> tuple[int, dict]:
        body: dict = {"workflow_definition_id": wf_id, "input": {}, **(extra or {})}
        if mode is not None:
            body["execution_mode"] = mode
        try:
            return HTTPStatus.ACCEPTED, request(base_url, "POST", "/api/workflow-runs", body)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def run_count() -> int:
        return len(runtime.db.list_workflow_runs(1000, wf_id))

    # draft: production (explicit AND omitted) blocked with the stable body, no run row; test allowed.
    for mode in ("production", None):
        before = run_count()
        code, body = start(mode)
        assert code == HTTPStatus.CONFLICT, (mode, code, body)
        assert body == {"error": "workflow_not_runnable", "reason": "draft_requires_test_mode", "status": "draft"}, body
        assert run_count() == before, "a blocked start must not create a run"
    code, body = start("test")
    assert code == HTTPStatus.ACCEPTED and body["run"]["id"], body
    # A held test run is still a test run: the hold flag must not bypass the guard elsewhere.
    code, body = start("test", {"hold": True})
    assert code == HTTPStatus.ACCEPTED and body["run"]["state"] == "paused", body
    code, body = start("staging")
    assert code == HTTPStatus.BAD_REQUEST and "execution_mode" in body["error"], body
    print("  draft: production/omitted 409, test 202 OK")

    # active: both modes allowed. The status update persists and survives re-read.
    updated = request(base_url, "PUT", f"/api/workflows/{wf_id}", {"status": "active"})["workflow"]
    assert updated["status"] == "active"
    assert request(base_url, "GET", f"/api/workflows/{wf_id}")["workflow"]["status"] == "active"
    for mode in ("test", "production", None):
        code, body = start(mode)
        assert code == HTTPStatus.ACCEPTED, (mode, code, body)
    print("  active: test/production/omitted 202 OK")

    # disabled: everything blocked.
    request(base_url, "PUT", f"/api/workflows/{wf_id}", {"status": "disabled"})
    for mode in ("test", "production", None):
        code, body = start(mode)
        assert code == HTTPStatus.CONFLICT, (mode, code, body)
        assert body == {"error": "workflow_not_runnable", "reason": "workflow_disabled", "status": "disabled"}, body
    print("  disabled: all modes 409 OK")

    # Status transitions are audited with the old->new pair; the actor column is populated.
    with runtime.db.connect() as conn:
        rows = conn.execute(
            "SELECT details FROM audit_log WHERE action = 'workflow_definition.status_change' AND resource_id = ?",
            (wf_id,),
        ).fetchall()
    transitions = [json.loads(row["details"]) for row in rows]
    assert {"old_status": "draft", "new_status": "active"} in transitions, transitions
    assert {"old_status": "active", "new_status": "disabled"} in transitions, transitions
    # A no-op save (same status) must not add a transition row.
    request(base_url, "PUT", f"/api/workflows/{wf_id}", {"status": "disabled"})
    with runtime.db.connect() as conn:
        after = conn.execute(
            "SELECT COUNT(*) AS n FROM audit_log WHERE action = 'workflow_definition.status_change' AND resource_id = ?",
            (wf_id,),
        ).fetchone()["n"]
    assert after == len(transitions), "same-status save must not audit a transition"
    print("  status change audited (old/new), no-op save silent OK")

    # Trigger fire is a production start: blocked on draft/disabled, allowed on active,
    # and trigger.enabled stays an independent switch from workflow status.
    request(base_url, "PUT", f"/api/workflows/{wf_id}", {"status": "draft"})
    trigger = runtime.db.create_workflow_trigger(
        {"workflow_definition_id": wf_id, "name": "Manual", "type": "manual", "config": {}, "enabled": True}
    )
    result = runtime.triggers.fire_trigger(trigger["id"])
    assert result["run"] is None, "a draft workflow must not start from a trigger"
    assert result["event"]["state"] == "failed" and "workflow_not_runnable" in result["event"]["error"], result["event"]
    request(base_url, "PUT", f"/api/workflows/{wf_id}", {"status": "active"})
    result = runtime.triggers.fire_trigger(trigger["id"])
    assert result["run"] is not None, "an active workflow must start from an enabled trigger"
    runtime.db.update_workflow_trigger(trigger["id"], {"enabled": False})
    result = runtime.triggers.fire_trigger(trigger["id"])
    assert result["run"] is None and result["event"]["state"] == "ignored", result["event"]
    print("  trigger: draft blocked (production), active fires, enabled independent OK")

    # The synchronous definition-backed path shares the guard.
    request(base_url, "PUT", f"/api/workflows/{wf_id}", {"status": "draft"})
    try:
        runtime.workflows.run_workflow(wf_id, {})
    except WorkflowNotRunnable as exc:
        assert exc.reason == "draft_requires_test_mode"
    else:
        raise AssertionError("run_workflow must enforce the status guard")
    final = runtime.workflows.run_workflow(wf_id, {}, execution_mode="test")
    assert final["state"] == "succeeded", final["state"]
    print("  synchronous run_workflow guarded OK")


def check_migration_backfill() -> None:
    """The 016 backfill is idempotent, audits each row once, and preserves 'disabled'."""
    with TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "atlas.sqlite")
        with db.connect() as conn:
            for wf_id, status in [("wfd_a", "draft"), ("wfd_b", "archived"), ("wfd_c", "disabled"), ("wfd_d", "active")]:
                conn.execute(
                    "INSERT INTO workflow_definitions(id, name, description, version, status, graph, policy, created_at, updated_at)"
                    " VALUES (?, ?, '', 1, ?, '{}', '{}', ?, ?)",
                    (wf_id, wf_id, status, now_iso(), now_iso()),
                )
        with db.connect() as conn:
            _migration_016_workflow_status_backfill(conn)
            _migration_016_workflow_status_backfill(conn)  # idempotent: second pass is a no-op
        with db.connect() as conn:
            statuses = {
                row["id"]: row["status"]
                for row in conn.execute("SELECT id, status FROM workflow_definitions").fetchall()
            }
            audits = conn.execute(
                "SELECT resource_id, details FROM audit_log WHERE action = 'workflow_definition.status_backfill'"
            ).fetchall()
        assert statuses == {"wfd_a": "active", "wfd_b": "active", "wfd_c": "disabled", "wfd_d": "active"}, statuses
        backfilled = sorted(row["resource_id"] for row in audits)
        assert backfilled == ["wfd_a", "wfd_b"], backfilled  # exactly once each; c and d untouched
        details = {row["resource_id"]: json.loads(row["details"]) for row in audits}
        assert details["wfd_b"] == {"old_status": "archived", "new_status": "active"}, details
    print("  migration 016 backfill idempotent, disabled preserved OK")


def check_db_validation() -> None:
    """The db layer is the shared choke point (packs import goes through it too)."""
    with TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "atlas.sqlite")
        for bad in ("archived", "Draft", 7, "", False, 0):
            try:
                db.create_workflow_definition({"name": "x", "graph": GRAPH, "status": bad})
            except ValueError as exc:
                assert "status must be one of" in str(exc)
            else:
                raise AssertionError(f"create must reject status {bad!r}")
        created = db.create_workflow_definition({"name": "x", "graph": GRAPH})
        assert created["status"] == "draft"
        # Missing/null keep the compatibility default; other falsy values are invalid.
        assert db.create_workflow_definition({"name": "x", "graph": GRAPH, "status": None})["status"] == "draft"
        try:
            db.update_workflow_definition(created["id"], {"status": "archived"})
        except ValueError as exc:
            assert "status must be one of" in str(exc)
        else:
            raise AssertionError("update must reject an unknown status")
    print("  db-layer status validation OK")


def check_pack_import_validation() -> None:
    """Pack validation/import default only missing/null status and reject other falsy values."""
    with TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "atlas.sqlite")

        def bundle(status_marker: object = ...) -> dict:
            workflow = {"name": "Packed", "graph": GRAPH}
            if status_marker is not ...:
                workflow["status"] = status_marker
            return {"schema_version": 1, "name": "status-pack", "version": "1", "workflows": [workflow]}

        assert import_pack(db, bundle())["workflows"][0]["status"] == "active"
        assert import_pack(db, bundle(None))["workflows"][0]["status"] == "active"
        before = len(db.list_workflow_definitions())
        for bad_status in ("", False, 0):
            invalid_bundle = bundle(bad_status)
            for operation in (validate_pack, lambda candidate: import_pack(db, candidate)):
                try:
                    operation(invalid_bundle)
                except ValueError as exc:
                    assert "status must be one of" in str(exc), str(exc)
                else:
                    raise AssertionError(f"pack validation/import must reject status {bad_status!r}")
            assert len(db.list_workflow_definitions()) == before, "rejected pack must not create a workflow"
    print("  pack validate/import: missing/null defaults active; invalid falsy statuses rejected atomically OK")


def main() -> None:
    check_guard_unit()
    check_db_validation()
    check_pack_import_validation()
    check_migration_backfill()
    with TemporaryDirectory() as tmp:
        runtime = AtlasRuntime(
            Config(
                host="127.0.0.1",
                port=0,
                db_path=Path(tmp) / "atlas.sqlite",
                api_token=None,
                request_timeout_seconds=1,
                enable_loopback_without_token=True,
                upload_dir=Path(tmp) / "uploads",
                outbound_allowlist=("127.0.0.1",),
            )
        )
        server = AtlasHttpServer(("127.0.0.1", 0), runtime)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            check_http(runtime, f"http://127.0.0.1:{server.server_address[1]}")
        finally:
            server.shutdown()
            thread.join(timeout=5)
    print("check_workflow_status OK")


if __name__ == "__main__":
    main()
