"""Hermetic checks for the audit-round fixes that are testable in-process (no network):
atomic terminal transitions, run graph/policy snapshots, trigger config validation, the
schedule-advance recovery, concurrent atomic secret writes, plus the round-6 cold-review
fixes (SSE session-id parsing, finalize source-state guard, CSV/env-file injection,
negative-units clamp, artifact ordering). Each uses its own temp DB.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import types
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.app import _LIMIT_CAP, _parse_limit, _validate_artifact_payload, cli_config
from atlas.byok import _write_env_file
from atlas.db import Database, _set_clause, atomic_write_0600, atomic_write_text, now_iso
from atlas.jobs import JobManager
from atlas.packs import _validate_pack_references
from atlas.thclaws_client import SseEvent, ThClawsError, extract_session_id, extract_text, iter_sse
from atlas.usage import usage_csv
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
    # Pin the `cancel_requested = 0` clause SPECIFICALLY: a job that is still 'queued' AND
    # cancel_requested=1 at once (write the column directly, NOT via mark_cancel_requested which
    # moves state off 'queued'). Without that clause, try_start_job would start it.
    raced = db.create_job({"worker_id": worker["id"], "prompt": "hi", "state": "queued"})
    db.update_job(raced["id"], cancel_requested=1)
    assert db.get_job(raced["id"])["state"] == "queued"
    assert db.try_start_job(raced["id"]) is False, "queued + cancel_requested must not start (the cancel clause)"
    assert db.get_job(raced["id"])["state"] == "queued"


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


def check_set_clause_guard() -> None:
    """_set_clause must reject any non-[a-z_] column name, making the f-string UPDATE builders
    injection-safe by construction (not merely because callers happen to pass literal names)."""
    assert _set_clause({"name": 1, "updated_at": 2}) == "name = ?, updated_at = ?"
    for bad in ("name=1; DROP", "a b", "Name", "col;", "x'", ""):
        try:
            _set_clause({bad: 1})
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe column name must be rejected: {bad!r}")


def check_atomic_write_text(tmp: Path) -> None:
    """atomic_write_text replaces atomically and never leaves a temp behind; a re-write keeps
    the file valid (durable-artifact write for CDR bills / signed usage exports)."""
    path = tmp / "artifact.json"
    atomic_write_text(path, '{"a":1}')
    assert path.read_text() == '{"a":1}'
    atomic_write_text(path, '{"a":2}')  # re-write to the same path
    assert path.read_text() == '{"a":2}'
    leftovers = [p.name for p in path.parent.glob(".artifact.json.tmp-*")]
    assert not leftovers, f"atomic_write_text left a temp file: {leftovers}"


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


def check_extract_session_id() -> None:
    """A bare "id" must NOT be read as a session id (it would corrupt the conversation binding
    and make the caller drop the frame's text); explicit session keys still work, and a frame
    can carry BOTH a session id and text."""
    # bare id on a normal event -> NOT a session; its text is still extractable.
    ev = SseEvent(event="message", data=json.dumps({"id": "msg-1", "text": "hello"}))
    assert extract_session_id(ev) is None, "bare 'id' must not be treated as a session id"
    assert extract_text(ev) == "hello"
    # explicit session id is honored, and combined session+text yields both.
    ev2 = SseEvent(event="message", data=json.dumps({"session_id": "s-1", "text": "world"}))
    assert extract_session_id(ev2) == "s-1"
    assert extract_text(ev2) == "world", "text in a session-carrying frame must not be lost"
    # id IS accepted only on an explicit session event.
    assert extract_session_id(SseEvent(event="session", data=json.dumps({"id": "s-2"}))) == "s-2"


def check_stop_requested(db: Database) -> None:
    """_stop_requested (the runner's pause/cancel halt) must report stop for paused / cancelled /
    waiting_for_human and NOT for running — directly, so a no-op'd predicate fails here."""
    runner = WorkflowRunner(db, JobManager(db))
    for state, expected in (("running", False), ("paused", True), ("waiting_for_human", True), ("cancelled", True)):
        run = db.create_workflow_run({"name": state, "state": state})
        assert runner._stop_requested(run["id"], [], {}) is expected, f"_stop_requested({state}) must be {expected}"


def check_finish_run_respects_state(db: Database) -> None:
    """The runner's _finish_run must NOT finalize (or emit run_finished) when the run was moved
    to paused/waiting_for_human — and MUST finalize a running run. Locks the won-guard the gate
    previously left green when removed."""
    runner = WorkflowRunner(db, JobManager(db))
    paused = db.create_workflow_run({"name": "p", "state": "paused"})
    runner._finish_run(paused["id"], "succeeded", {})
    assert db.get_workflow_run(paused["id"])["state"] == "paused", "runner must not overwrite a paused run"
    assert not any(e["event_type"] == "run_finished" for e in db.list_workflow_events(paused["id"])), "no run_finished on a lost finalize"
    running = db.create_workflow_run({"name": "r", "state": "running"})
    runner._finish_run(running["id"], "succeeded", {})
    assert db.get_workflow_run(running["id"])["state"] == "succeeded"
    assert any(e["event_type"] == "run_finished" for e in db.list_workflow_events(running["id"])), "running run must emit run_finished"


def check_finalize_allowed_from(db: Database) -> None:
    """The runner's finish must only transition from its allowed source state(s); it must NOT
    clobber a run that another path moved to paused/waiting_for_human."""
    paused = db.create_workflow_run({"name": "p", "state": "paused"})
    # allowed_from=('running',) -> a paused run is NOT finalized by the runner path.
    assert db.finalize_workflow_run(paused["id"], "succeeded", allowed_from=("running",)) is False
    assert db.get_workflow_run(paused["id"])["state"] == "paused", "paused run must not be overwritten"
    # default (no allowed_from) -> cancel can still override a paused run.
    assert db.finalize_workflow_run(paused["id"], "cancelled") is True
    assert db.get_workflow_run(paused["id"])["state"] == "cancelled"
    # reject's source (waiting_for_human) is permitted when listed.
    waiting = db.create_workflow_run({"name": "w", "state": "waiting_for_human"})
    assert db.finalize_workflow_run(waiting["id"], "failed", allowed_from=("waiting_for_human", "running")) is True


def check_csv_injection() -> None:
    """usage_csv must neutralize spreadsheet formula injection in free-text fields."""
    rows = usage_csv([{ "kind": "job", "actor": "=HYPERLINK(\"http://evil\")", "model": "+1+1", "status": "ok" }])
    line = [r for r in rows.splitlines() if "job" in r][-1]
    assert "'=HYPERLINK" in line, "leading = must be escaped"
    assert "'+1+1" in line, "leading + must be escaped"
    # a benign value is untouched.
    benign = usage_csv([{ "kind": "job", "actor": "alice", "status": "ok" }])
    assert ",alice," in benign or benign.strip().endswith("alice") or "alice" in benign
    assert "'alice" not in benign


def check_byok_newline(tmp: Path) -> None:
    """_write_env_file must reject a newline in the key (env-file line injection) and a bad env var name."""
    path = tmp / "worker.env"
    for bad_key in ("sk-real\nINJECTED=1", "sk\rINJECTED=1"):
        try:
            _write_env_file(path, "OPENAI_API_KEY", bad_key)
        except ValueError:
            pass
        else:
            raise AssertionError("newline in key must be rejected")
    for bad_var in ("FOO=BAR", "FOO\nBAR"):
        try:
            _write_env_file(path, bad_var, "value")
        except ValueError:
            pass
        else:
            raise AssertionError("bad env var name must be rejected")
    # a clean write still works.
    _write_env_file(path, "OPENAI_API_KEY", "sk-clean")
    assert path.read_text().strip() == "OPENAI_API_KEY=sk-clean"


def check_negative_units_clamped(db: Database) -> None:
    """emit_usage_event must clamp a negative units to 0 so it can't deflate the billed total."""
    db.emit_usage_event({"idempotency_key": "neg-1", "kind": "workflow_run", "units": -1000, "status": "succeeded"})
    row = next(e for e in db.list_usage_events() if e["idempotency_key"] == "neg-1")
    assert row["units"] == 0, f"negative units must clamp to 0, got {row['units']}"


def check_artifact_ordering(db: Database) -> None:
    """Same-second artifacts on the same key must resolve last-write-wins by insertion order
    (rowid tiebreaker), not undefined."""
    run = db.create_workflow_run({"name": "a", "state": "running"})
    db.create_artifact({"run_id": run["id"], "key": "k", "content": "first"})
    db.create_artifact({"run_id": run["id"], "key": "k", "content": "second"})
    # Force identical created_at on both rows so only the rowid tiebreaker can order them.
    with db.connect() as conn:
        conn.execute("UPDATE artifacts SET created_at = ? WHERE run_id = ?", (now_iso(), run["id"]))
    listed = db.list_artifacts(run_id=run["id"], limit=10)
    assert listed[0]["content"] == "second", f"rowid tiebreaker must put newest first, got {listed[0]['content']}"


def check_iter_sse_deadline() -> None:
    """iter_sse enforces the deadline per LINE, so a heartbeat-only stream (`: ping`, no events)
    can't pin the thread past the deadline."""
    try:
        list(iter_sse(iter([b": ping\n"] * 100), stream_deadline=time.monotonic() - 1))
    except ThClawsError as exc:
        assert "deadline" in str(exc), exc
    else:
        raise AssertionError("heartbeat-only stream past the deadline must raise")
    assert list(iter_sse(iter([b": ping\n", b": ping\n"]))) == [], "heartbeats with no deadline are just skipped"


def check_resume_rearm(db: Database) -> None:
    """If a runner thread exits while the run is 'running' (a resume/approve handoff race),
    _run_background must re-arm a runner so the run isn't stranded 'running' with no runner."""
    runner = WorkflowRunner(db, JobManager(db))
    run = db.create_workflow_run({"name": "r", "state": "running", "graph_snapshot": GRAPH, "policy_snapshot": {}})
    calls = {"n": 0}

    def fake_execute(run_id: str, graph: dict, policy: dict, input: dict) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            return  # first thread exits leaving the run 'running' (simulates the race)
        db.finalize_workflow_run(run_id, "succeeded", allowed_from=("running",))  # re-armed thread converges

    runner._execute_run = fake_execute  # type: ignore[assignment]
    runner._threads[run["id"]] = threading.current_thread()  # so the finally's pop-condition matches
    runner._run_background(run["id"], GRAPH, {}, {})
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and db.get_workflow_run(run["id"])["state"] == "running":
        time.sleep(0.01)
    assert db.get_workflow_run(run["id"])["state"] == "succeeded", "resume race must re-arm a runner, not strand 'running'"
    assert calls["n"] >= 2, "the re-armed runner must actually run"


def check_pack_workspace_ownership(db: Database) -> None:
    """Pack import must reject a node that pins a worker AND a workspace owned by a different
    worker (else the router silently runs on the workspace's owner, ignoring the declared worker)."""
    w1 = db.upsert_worker({"base_url": "http://127.0.0.1:1", "name": "w1"})
    w2 = db.upsert_worker({"base_url": "http://127.0.0.1:2", "name": "w2"})
    ws2 = db.upsert_workspace({"worker_id": w2["id"], "workspace_key": "k2", "workspace_dir": "/tmp/x"})
    mismatch = {"workflows": [{"graph": {"nodes": [{"id": "n", "worker_id": w1["id"], "workspace_id": ws2["id"]}]}}]}
    try:
        _validate_pack_references(db, mismatch)
    except ValueError as exc:
        assert "does not belong" in str(exc), exc
    else:
        raise AssertionError("pack with mismatched worker/workspace must be rejected")
    ws1 = db.upsert_workspace({"worker_id": w1["id"], "workspace_key": "k1", "workspace_dir": "/tmp/y"})
    _validate_pack_references(db, {"workflows": [{"graph": {"nodes": [{"id": "n", "worker_id": w1["id"], "workspace_id": ws1["id"]}]}}]})


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
        check_extract_session_id()
        check_stop_requested(Database(Path(tmp) / "stopreq.sqlite"))
        check_finish_run_respects_state(Database(Path(tmp) / "finishstate.sqlite"))
        check_finalize_allowed_from(Database(Path(tmp) / "finalfrom.sqlite"))
        check_csv_injection()
        check_byok_newline(Path(tmp))
        check_negative_units_clamped(Database(Path(tmp) / "units.sqlite"))
        check_artifact_ordering(Database(Path(tmp) / "artord.sqlite"))
        check_iter_sse_deadline()
        check_resume_rearm(Database(Path(tmp) / "rearm.sqlite"))
        check_pack_workspace_ownership(Database(Path(tmp) / "packws.sqlite"))
        check_set_clause_guard()
        check_atomic_write_text(Path(tmp))
    print("audit fixes check ok")


if __name__ == "__main__":
    main()
