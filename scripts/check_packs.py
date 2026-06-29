from __future__ import annotations

import sys
import time
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.db import Database, now_iso
from atlas.packs import (
    PACKS_DIR,
    export_pack,
    import_pack,
    list_available_packs,
    load_pack_file,
    sign_pack,
    validate_pack,
    verify_pack_signature,
)
from atlas.workflows import WorkflowRunner

GOV_PACK = PACKS_DIR / "gov_complaint.json"


class FakeJobService:
    """Mock worker: completes every job immediately (routing-agnostic)."""

    def __init__(self, db: Database, worker_id: str):
        self.db = db
        self.worker_id = worker_id
        self.prompts: list[str] = []

    def submit(self, payload: dict) -> dict:
        prompt = payload["prompt"]
        self.prompts.append(prompt)
        job = self.db.create_job({"worker_id": self.worker_id, "prompt": prompt, "state": "running"})
        self.db.append_job_text(job["id"], f"result: {prompt}")
        self.db.update_job(job["id"], state="succeeded", finished_at=now_iso())
        return self.db.get_job(job["id"]) or job


def wait_for_run(db: Database, run_id: str, state: str) -> dict:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        run = db.get_workflow_run(run_id)
        if run and run["state"] == state:
            return run
        time.sleep(0.01)
    raise AssertionError(f"run did not reach {state}: {db.get_workflow_run(run_id)}")


def assert_rejected(bundle, needle: str) -> None:
    try:
        validate_pack(bundle)
    except ValueError as exc:
        assert needle in str(exc), str(exc)
        return
    raise AssertionError(f"expected ValueError containing: {needle}")


def main() -> None:
    bundle = load_pack_file(GOV_PACK)

    # 1. The shipped gov pack validates clean.
    validate_pack(bundle)

    with TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "atlas.sqlite")

        # 2. Importing creates the definition(s) + trigger(s); graphs pass the validator.
        result = import_pack(db, bundle)
        assert len(result["workflows"]) == 1, result
        assert len(result["triggers"]) == 1, result
        definition = result["workflows"][0]
        assert db.get_workflow_definition(definition["id"]) is not None
        triggers = db.list_workflow_triggers(workflow_definition_id=definition["id"])
        assert triggers and triggers[0]["name"] == "Citizen complaint intake"

        # 3. A run from sample_input reaches the human gate, is approved, and finishes
        #    end-to-end on a mock worker.
        # One mock worker tagged with every node role so role-based routing resolves.
        worker = db.upsert_worker(
            {
                "name": "Mock",
                "base_url": "http://127.0.0.1:1",
                "tags": ["triage_officer", "case_officer", "comms_officer"],
            }
        )
        jobs = FakeJobService(db, worker["id"])
        runner = WorkflowRunner(db, jobs, poll_interval_seconds=0)
        waiting = runner.run_workflow(definition["id"], bundle["sample_input"])
        assert waiting["state"] == "waiting_for_human", waiting
        pending = db.list_approvals(state="pending", run_id=waiting["id"])
        assert len(pending) == 1, pending
        runner.choose_approval(pending[0]["id"], "approve")
        finished = wait_for_run(db, waiting["id"], "succeeded")
        assert finished["counters"]["completed_nodes"] == ["triage", "draft", "review", "publish"], finished["counters"]
        assert jobs.prompts[-1].startswith("Publish the approved response")

        # 4. Export round-trips to an equivalent bundle.
        exported = export_pack(db, definition["id"])
        validate_pack(exported)
        assert exported["workflows"][0]["graph"] == bundle["workflows"][0]["graph"]
        assert exported["triggers"][0]["name"] == "Citizen complaint intake"
        # Re-importing the exported bundle yields another working definition.
        reimported = import_pack(db, exported)
        assert reimported["workflows"][0]["graph"] == bundle["workflows"][0]["graph"]

        # M8: signing — sign, verify, import; tampered/wrong-key/unsigned per policy.
        secret = "pack-signing-secret"
        signed = sign_pack(bundle, secret)
        assert verify_pack_signature(signed, secret) is True
        assert verify_pack_signature(signed, "wrong-key") is False
        assert verify_pack_signature(bundle, secret) is False  # unsigned bundle
        assert import_pack(db, signed, secret_key=secret)["workflows"], "signed pack should import"

        tampered = deepcopy(signed)
        tampered["workflows"][0]["name"] = "Tampered handler"
        try:
            import_pack(db, tampered, secret_key=secret)
        except ValueError as exc:
            assert "signature is invalid" in str(exc), str(exc)
        else:
            raise AssertionError("tampered signed pack must be rejected")

        try:
            import_pack(db, signed, secret_key="wrong-key")
        except ValueError as exc:
            assert "signature is invalid" in str(exc), str(exc)
        else:
            raise AssertionError("signed pack with the wrong key must be rejected")

        # Unsigned packs import unless a signature is required.
        assert import_pack(db, bundle)["workflows"]
        try:
            import_pack(db, bundle, require_signature=True)
        except ValueError as exc:
            assert "unsigned" in str(exc), str(exc)
        else:
            raise AssertionError("require_signature must reject an unsigned pack")

        # Export preserves the definition version (no silent downgrade on round-trip).
        versioned = db.create_workflow_definition(
            {"name": "V2", "version": 2, "graph": bundle["workflows"][0]["graph"], "policy": {}}
        )
        exported_v2 = export_pack(db, versioned["id"])
        assert exported_v2["workflows"][0]["version"] == 2, exported_v2["workflows"][0]
        assert import_pack(db, exported_v2)["workflows"][0]["version"] == 2

        # A pack with a concrete worker_id that doesn't exist here is rejected on import
        # (role-only nodes stay portable).
        dangling = deepcopy(bundle)
        dangling["workflows"][0]["graph"]["nodes"][0]["worker_id"] = "wrk_does_not_exist"
        try:
            import_pack(db, dangling)
        except ValueError as exc:
            assert "unknown worker_id" in str(exc), str(exc)
        else:
            raise AssertionError("pack with an unknown concrete worker_id must be rejected")

    # The local registry listing reports the signed flag (shipped gov pack is unsigned).
    gov = next(entry for entry in list_available_packs() if entry.get("name") == "gov_complaint")
    assert gov["signed"] is False, gov

    # 5. Invalid packs are rejected with clear errors.
    assert_rejected({"name": "x", "version": "1", "workflows": []}, "schema_version must be 1")
    assert_rejected({"schema_version": 1, "version": "1", "workflows": [{"name": "n", "graph": {}}]}, "non-empty name")

    bad_node = deepcopy(bundle)
    bad_node["workflows"][0]["graph"]["nodes"][0]["type"] = "wizard"
    assert_rejected(bad_node, "unsupported type")

    bad_edge = deepcopy(bundle)
    bad_edge["workflows"][0]["graph"]["edges"][0]["to"] = "nowhere"
    assert_rejected(bad_edge, "missing to node")

    bad_role = deepcopy(bundle)
    bad_role["roles"] = ["operator", "superuser"]
    assert_rejected(bad_role, "not a known RBAC role")

    non_string_role = deepcopy(bundle)
    non_string_role["roles"] = [{}]  # unhashable: must be a clean ValueError, not a 500
    assert_rejected(non_string_role, "not a known RBAC role")

    bad_trigger = deepcopy(bundle)
    bad_trigger["triggers"][0]["type"] = "smoke_signal"
    assert_rejected(bad_trigger, "unsupported workflow trigger type")

    over_cap = deepcopy(bundle)
    over_cap["workflows"][0]["policy"] = {"max_jobs": 1000000000}
    assert_rejected(over_cap, "max_jobs must be an integer between 1 and 100")

    # 6. Schedule triggers get a next_fire_at on import; enabled survives a round-trip.
    with TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "atlas.sqlite")
        synthetic = {
            "schema_version": 1,
            "name": "sched",
            "version": "1.0.0",
            "workflows": [
                {"name": "S", "graph": {"start": "a", "nodes": [{"id": "a", "type": "human_gate"}], "edges": []}}
            ],
            "triggers": [
                {"workflow": 0, "name": "Nightly", "type": "schedule", "config": {"daily_time": "02:00"}, "enabled": False}
            ],
        }
        result = import_pack(db, synthetic)
        trigger = result["triggers"][0]
        assert trigger["next_fire_at"], "schedule trigger must get a next_fire_at on import"
        assert trigger["enabled"] == 0, trigger["enabled"]
        exported = export_pack(db, result["workflows"][0]["id"])
        assert exported["triggers"][0]["enabled"] is False, exported["triggers"][0]

    # 7. A non-integer workflow version is rejected up front, and the failed import is
    #    atomic — validate_pack runs before any write, so no partial workflow is left.
    a_node = {"start": "a", "nodes": [{"id": "a", "type": "human_gate"}], "edges": []}
    bad_version = {
        "schema_version": 1,
        "name": "bad-version",
        "version": "1.0.0",
        "workflows": [
            {"name": "Good", "version": 1, "graph": deepcopy(a_node)},
            {"name": "Bad", "version": "not-an-int", "graph": deepcopy(a_node)},
        ],
    }
    assert_rejected(bad_version, "non-integer version")
    with TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "atlas.sqlite")
        try:
            import_pack(db, bad_version)
        except ValueError:
            pass
        else:
            raise AssertionError("import_pack must reject a bad-version bundle")
        assert db.list_workflow_definitions() == [], "rejected import must leave no partial workflows"

    # 8. Atomic rollback when a write fails mid-import (after validation passes): a forced
    #    failure on the second workflow write must undo the first.
    with TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "atlas.sqlite")
        two_ok = {
            "schema_version": 1,
            "name": "two",
            "version": "1.0.0",
            "workflows": [
                {"name": "One", "graph": deepcopy(a_node)},
                {"name": "Two", "graph": deepcopy(a_node)},
            ],
        }
        original = db.create_workflow_definition
        calls = {"n": 0}

        def failing_create(payload, _original=original, _calls=calls):
            _calls["n"] += 1
            if _calls["n"] == 2:
                raise RuntimeError("induced mid-write failure")
            return _original(payload)

        db.create_workflow_definition = failing_create
        try:
            import_pack(db, two_ok)
        except RuntimeError:
            pass
        else:
            raise AssertionError("import_pack must propagate a mid-write failure")
        db.create_workflow_definition = original
        assert db.list_workflow_definitions() == [], "mid-write failure must roll back the first workflow"

    print("packs check ok")


if __name__ == "__main__":
    main()
