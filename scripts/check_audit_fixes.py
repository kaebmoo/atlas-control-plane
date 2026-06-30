"""Hermetic checks for the audit-round fixes that are testable in-process (no network):
atomic terminal transitions, run graph/policy snapshots, trigger config validation, the
schedule-advance recovery, and concurrent atomic secret writes. Each uses its own temp DB.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import types
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.app import _LIMIT_CAP, _parse_limit, _validate_artifact_payload, cli_config
from atlas.db import Database, atomic_write_0600, now_iso
from atlas.jobs import JobManager
from atlas.workflows import (
    WorkflowRunner,
    WorkflowTriggerService,
    _parse_utc,
    validate_workflow_trigger_payload,
)

GRAPH = {"start": "a", "nodes": [{"id": "a", "type": "human_gate", "label": "Approve"}], "edges": []}


def check_finalize_is_conditional(db: Database) -> None:
    """A terminal transition must be atomic: once a run is cancelled, a finishing thread's
    succeeded/failed write must be rejected, not overwrite the cancel."""
    run = db.create_workflow_run({"name": "r", "state": "running"})
    assert db.finalize_workflow_run(run["id"], "cancelled") is True
    assert db.get_workflow_run(run["id"])["state"] == "cancelled"
    # The racing finisher loses: no overwrite of the terminal cancel.
    assert db.finalize_workflow_run(run["id"], "succeeded") is False
    assert db.get_workflow_run(run["id"])["state"] == "cancelled"
    # A fresh run still finalizes normally.
    other = db.create_workflow_run({"name": "r2", "state": "running"})
    assert db.finalize_workflow_run(other["id"], "succeeded") is True


def check_run_snapshot(db: Database) -> None:
    """A run executes the graph/policy it started on, even after the live definition is edited
    or deleted: the snapshot is persisted and the resolver prefers it."""
    definition = db.create_workflow_definition({"name": "S", "graph": GRAPH, "policy": {"max_jobs": 3}})
    runner = WorkflowRunner(db, JobManager(db))
    run = runner._create_run(GRAPH, {"max_jobs": 3}, {"topic": "x"}, definition["id"], "run")
    stored = db.get_workflow_run(run["id"])
    assert stored["graph_snapshot"]["start"] == "a", "graph snapshot must persist"
    assert stored["policy_snapshot"]["max_jobs"] == 3, "policy snapshot must persist"
    # Deleting the definition must NOT strand the run: the resolver returns the snapshot.
    db.delete_workflow_definition(definition["id"])
    assert db.get_workflow_definition(definition["id"]) is None
    graph, policy = runner._run_graph_policy(db.get_workflow_run(run["id"]))
    assert graph["start"] == "a" and policy["max_jobs"] == 3, "resolver must use the snapshot"
    # A legacy run with no snapshot AND no resolvable definition cannot resolve.
    legacy = db.create_workflow_run({"name": "legacy", "state": "paused"})
    try:
        runner._run_graph_policy(db.get_workflow_run(legacy["id"]))
    except ValueError:
        pass
    else:
        raise AssertionError("a run with neither snapshot nor definition must raise")


def check_trigger_config_keys() -> None:
    """A misspelled filter key on a CLOSED-config trigger must be rejected, not silently widen
    it to match all — but manual/webhook keep an OPEN config (per the schema) and must accept
    arbitrary keys (additive API contract)."""
    validate_workflow_trigger_payload({"type": "artifact_created", "config": {"key": "invoice"}})
    try:
        validate_workflow_trigger_payload({"type": "artifact_created", "config": {"kee": "invoice"}})
    except ValueError as exc:
        assert "unknown" in str(exc).lower(), exc
    else:
        raise AssertionError("an unknown closed-config trigger key must be rejected")
    # Open configs: must NOT be rejected (would break the published manual/webhook contract).
    validate_workflow_trigger_payload({"type": "manual", "config": {"source": "ui"}})
    validate_workflow_trigger_payload({"type": "webhook", "config": {"secret_ref": "hook"}})


def check_try_start_job(db: Database) -> None:
    """try_start_job atomically claims queued->running only when not cancelled — the
    check-and-set that closes the cancel/dispatch TOCTOU race."""
    worker = db.upsert_worker({"base_url": "http://127.0.0.1:12", "name": "w"})
    fresh = db.create_job({"worker_id": worker["id"], "prompt": "hi", "state": "queued"})
    assert db.try_start_job(fresh["id"]) is True
    assert db.get_job(fresh["id"])["state"] == "running"
    assert db.try_start_job(fresh["id"]) is False  # already running, not re-claimable
    cancelled = db.create_job({"worker_id": worker["id"], "prompt": "hi", "state": "queued"})
    db.mark_cancel_requested(cancelled["id"])
    assert db.try_start_job(cancelled["id"]) is False, "a cancelled queued job must not start"
    assert db.get_job(cancelled["id"])["state"] != "running"


def check_cancel_terminal_guard(db: Database) -> None:
    """A cancel landing after a job already completed must NOT regress its terminal state to
    cancel_requested — mark_cancel_requested is conditional on a non-terminal state."""
    worker = db.upsert_worker({"base_url": "http://127.0.0.1:13", "name": "w"})
    done = db.create_job({"worker_id": worker["id"], "prompt": "hi", "state": "queued"})
    db.update_job(done["id"], state="succeeded", finished_at=now_iso())
    assert db.mark_cancel_requested(done["id"]) is False, "cancel must lose against a completed job"
    assert db.get_job(done["id"])["state"] == "succeeded", "terminal state must not regress"
    live = db.create_job({"worker_id": worker["id"], "prompt": "hi", "state": "running"})
    assert db.mark_cancel_requested(live["id"]) is True
    assert db.get_job(live["id"])["state"] == "cancel_requested"


def check_workflow_input_type(db: Database) -> None:
    """A non-object workflow input must be rejected at the runner boundary (covers trigger-
    fired and library runs, not just the HTTP handler) before any run is created. Only None is
    normalized to {}; falsy non-objects ([], "", 0, False) must NOT slip through as {}."""
    runner = WorkflowRunner(db, JobManager(db))
    definition = db.create_workflow_definition({"name": "I", "graph": GRAPH, "policy": {}})
    before = len(db.list_workflow_runs(limit=10))
    for bad in (["not", "an", "object"], [], "", 0, False):
        try:
            runner.start_workflow(definition["id"], bad)
        except ValueError as exc:
            assert "must be an object" in str(exc), (bad, exc)
        else:
            raise AssertionError(f"a non-object workflow input must be rejected: {bad!r}")
    assert len(db.list_workflow_runs(limit=10)) == before, "rejected input must not create a run"


def check_trigger_payload_type(db: Database) -> None:
    """fire_trigger is the shared service boundary: a non-object payload ([], "", 0, False)
    must be rejected, not coerced to {} and persisted as a run's input."""
    runner = WorkflowRunner(db, JobManager(db))
    triggers = WorkflowTriggerService(db, runner)
    definition = db.create_workflow_definition({"name": "T", "graph": GRAPH, "policy": {}})
    trigger = db.create_workflow_trigger(
        {"workflow_definition_id": definition["id"], "name": "m", "type": "manual", "enabled": True}
    )
    before = len(db.list_workflow_runs(limit=10))
    for bad in ([], "", 0, False):
        try:
            triggers.fire_trigger(trigger["id"], bad)
        except ValueError as exc:
            assert "must be an object" in str(exc), (bad, exc)
        else:
            raise AssertionError(f"a non-object trigger payload must be rejected: {bad!r}")
    assert len(db.list_workflow_runs(limit=10)) == before, "rejected payload must not create a run"


def check_schedule_advances_past_stuck_claim(db: Database) -> None:
    """A schedule slot already claimed (crash after claim, before the run started) must not
    wedge the schedule forever: the tick advances next_fire_at past the stuck slot and starts
    no duplicate run."""
    runner = WorkflowRunner(db, JobManager(db))
    triggers = WorkflowTriggerService(db, runner)
    definition = db.create_workflow_definition({"name": "Sched", "graph": GRAPH, "policy": {}})
    past = "2000-01-01T00:00:00Z"
    trigger = db.create_workflow_trigger(
        {
            "workflow_definition_id": definition["id"],
            "name": "sched",
            "type": "schedule",
            "enabled": True,
            "config": {"interval_minutes": 15},
            "next_fire_at": past,
        }
    )
    # Simulate the crash: the slot's dedupe key is already claimed.
    assert db.claim_trigger_dedupe(trigger["id"], f"{trigger['id']}:{past}", {}) is True
    runs_before = len(db.list_workflow_runs(limit=10))

    triggers.scheduler_tick()

    updated = db.get_workflow_trigger(trigger["id"])
    assert updated["next_fire_at"] != past, "stuck schedule slot must advance"
    assert _parse_utc(updated["next_fire_at"]) > datetime.now(UTC), "advanced slot must be in the future"
    assert len(db.list_workflow_runs(limit=10)) == runs_before, "a duplicate slot must not start a run"


def check_atomic_write_concurrent() -> None:
    """Concurrent atomic_write_0600 to one path must not raise (unique temp per call) and must
    leave a valid 0600 file — no FileNotFoundError from a shared PID-only temp name."""
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "secret.json"
        errors: list[Exception] = []

        def writer(i: int) -> None:
            try:
                for _ in range(25):
                    atomic_write_0600(path, json.dumps({"w": i}).encode("utf-8"))
            except Exception as exc:  # noqa: BLE001 - the test records, then asserts none
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors, f"concurrent atomic writes raised: {errors[:2]}"
        json.loads(path.read_text(encoding="utf-8"))  # final file is intact JSON
        assert oct(path.stat().st_mode)[-3:] == "600"


def check_artifact_cross_run(db: Database) -> None:
    """An artifact's job_id must belong to its run; a job that ran under a different run must be
    rejected (cross-run reference)."""
    run_a = db.create_workflow_run({"name": "A", "state": "running"})
    run_b = db.create_workflow_run({"name": "B", "state": "running"})
    worker = db.upsert_worker({"base_url": "http://127.0.0.1:11", "name": "w"})
    job = db.create_job({"worker_id": worker["id"], "prompt": "hi"})
    db.create_workflow_node({"run_id": run_b["id"], "node_key": "n", "job_id": job["id"]})
    runtime = types.SimpleNamespace(db=db)
    # Same-run attachment is fine.
    _validate_artifact_payload(runtime, {"run_id": run_b["id"], "job_id": job["id"], "kind": "text"})
    # Cross-run attachment is rejected.
    try:
        _validate_artifact_payload(runtime, {"run_id": run_a["id"], "job_id": job["id"], "kind": "text"})
    except ValueError as exc:
        assert "does not belong" in str(exc), exc
    else:
        raise AssertionError("a job from another run must not attach an artifact here")
    # A standalone job (no workflow context at all) must also be rejected, not slip through.
    standalone = db.create_job({"worker_id": worker["id"], "prompt": "hi"})
    try:
        _validate_artifact_payload(runtime, {"run_id": run_b["id"], "job_id": standalone["id"], "kind": "text"})
    except ValueError as exc:
        assert "does not belong" in str(exc), exc
    else:
        raise AssertionError("a standalone job (no run context) must not attach an artifact")


def check_cli_config_preserves_fields() -> None:
    """CLI overrides (run.sh / run-prod.sh pass --port etc.) must not drop Config fields such
    as require_signed_packs — otherwise the launch scripts silently disable the policy."""
    prev = os.environ.get("ATLAS_REQUIRE_SIGNED_PACKS")
    os.environ["ATLAS_REQUIRE_SIGNED_PACKS"] = "true"
    try:
        config = cli_config(["--port", "9999"])
        assert config.port == 9999
        assert config.require_signed_packs is True, "CLI override dropped require_signed_packs"
    finally:
        if prev is None:
            os.environ.pop("ATLAS_REQUIRE_SIGNED_PACKS", None)
        else:
            os.environ["ATLAS_REQUIRE_SIGNED_PACKS"] = prev


def check_limit_clamp() -> None:
    """?limit must be clamped to [1, cap]: a raw negative/zero would disable SQLite's LIMIT."""
    assert _parse_limit({"limit": ["-1"]}) == 1
    assert _parse_limit({"limit": ["0"]}) == 1
    assert _parse_limit({"limit": ["5"]}) == 5
    assert _parse_limit({"limit": ["99999999"]}) == _LIMIT_CAP
    assert _parse_limit({}) == 100
    assert _parse_limit({"limit": ["abc"]}) == 100


def main() -> None:
    with TemporaryDirectory() as tmp:
        check_finalize_is_conditional(Database(Path(tmp) / "finalize.sqlite"))
        check_run_snapshot(Database(Path(tmp) / "snapshot.sqlite"))
        check_trigger_config_keys()
        check_trigger_payload_type(Database(Path(tmp) / "trigpayload.sqlite"))
        check_schedule_advances_past_stuck_claim(Database(Path(tmp) / "sched.sqlite"))
        check_atomic_write_concurrent()
        check_artifact_cross_run(Database(Path(tmp) / "artifact.sqlite"))
        check_limit_clamp()
        check_try_start_job(Database(Path(tmp) / "trystart.sqlite"))
        check_cancel_terminal_guard(Database(Path(tmp) / "cancelguard.sqlite"))
        check_workflow_input_type(Database(Path(tmp) / "inputtype.sqlite"))
        check_cli_config_preserves_fields()
    print("audit fixes check ok")


if __name__ == "__main__":
    main()
