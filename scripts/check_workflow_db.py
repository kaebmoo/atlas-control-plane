from __future__ import annotations

import sys
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.db import Database


def main() -> None:
    with TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "atlas.sqlite")
        with db.connect() as conn:
            tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            approval_columns = {row["name"] for row in conn.execute("PRAGMA table_info(approvals)")}
        assert {
            "workflow_definitions",
            "workflow_runs",
            "workflow_nodes",
            "workflow_edges",
            "workflow_events",
            "approvals",
            "artifacts",
            "workflow_triggers",
            "workflow_trigger_events",
        } <= tables
        assert {"choices", "selected_choice"} <= approval_columns

        definition = db.create_workflow_definition(
            {
                "name": "Smoke",
                "graph": {"start": "a", "nodes": [], "edges": []},
                "policy": {"max_jobs": 1},
                "default_reply": {"mode": "none"},
            }
        )
        assert definition["graph"]["start"] == "a"
        assert definition["default_reply"] == {"mode": "none"}
        assert db.list_workflow_definitions()[0]["id"] == definition["id"]
        assert db.update_workflow_definition(definition["id"], {"status": "active"})["status"] == "active"
        assert db.update_workflow_definition(definition["id"], {"default_reply": None})["default_reply"] is None
        # Regression: an explicit None graph/policy is encoded (NOT NULL columns), not
        # written as SQL NULL — it reads back as None and downstream treats it as {}.
        assert db.update_workflow_definition(definition["id"], {"policy": None})["policy"] is None
        assert db.update_workflow_definition(definition["id"], {"policy": {"max_jobs": 1}})["policy"] == {"max_jobs": 1}

        run = db.create_workflow_run({"workflow_definition_id": definition["id"], "input": {"topic": "x"}})
        assert db.get_workflow_run(run["id"])["input"]["topic"] == "x"
        assert db.list_workflow_runs(workflow_definition_id=definition["id"])[0]["id"] == run["id"]
        event = db.append_workflow_event(run["id"], "node_started", {"attempt": 1}, node_key="a")
        events = db.list_workflow_events(run["id"])
        assert [item["seq"] for item in events] == [1, 2]
        assert [item["event_type"] for item in events] == ["created", "node_started"]
        assert event["node_key"] == "a"

        approval = db.create_approval(
            {
                "run_id": run["id"],
                "node_key": "a",
                "approval_key": "human_gate:a:1",
                "label": "Approve A",
            }
        )
        duplicate = db.create_approval(
            {
                "run_id": run["id"],
                "node_key": "a",
                "approval_key": "human_gate:a:1",
            }
        )
        assert duplicate["id"] == approval["id"]
        assert db.list_approvals(state="pending")[0]["id"] == approval["id"]
        assert db.decide_approval(approval["id"], "approved")["state"] == "approved"
        try:
            db.decide_approval(approval["id"], "rejected")
        except ValueError as exc:
            assert "already approved" in str(exc)
        else:
            raise AssertionError("deciding an approval twice must fail")

        choice_approval = db.create_approval(
            {
                "run_id": run["id"],
                "node_key": "choose",
                "approval_key": "human_gate:choose:1",
                "choices": [{"id": "left", "label": "Left"}, {"id": "right", "label": "Right"}],
            }
        )
        chosen = db.choose_approval(choice_approval["id"], "right")
        assert chosen["state"] == "chosen" and chosen["selected_choice"] == "right"

        trigger = db.create_workflow_trigger({"workflow_definition_id": definition["id"], "name": "Manual", "type": "manual"})
        event = db.append_workflow_trigger_event(trigger["id"], "received", {"topic": "x"}, run_id=run["id"], dedupe_key="one")
        assert db.list_workflow_trigger_events(trigger["id"])[0]["id"] == event["id"]

        # atomic dedupe claim: only the first claim of a (trigger, dedupe_key) wins, and N
        # threads racing the same fresh key yield exactly one winner (no double trigger run).
        assert db.claim_trigger_dedupe(trigger["id"], "claim-1", {"x": 1}) is True
        assert db.claim_trigger_dedupe(trigger["id"], "claim-1", {"x": 2}) is False
        race_results: list[bool] = []
        start = threading.Barrier(8)

        def _race() -> None:
            start.wait()
            race_results.append(db.claim_trigger_dedupe(trigger["id"], "claim-race"))

        racers = [threading.Thread(target=_race) for _ in range(8)]
        for thread in racers:
            thread.start()
        for thread in racers:
            thread.join()
        assert sum(1 for won in race_results if won) == 1, f"exactly one claim must win: {race_results}"

        artifact = db.create_artifact({"run_id": run["id"], "key": "notes", "content": "ok"})
        assert db.list_artifacts(run_id=run["id"])[0]["id"] == artifact["id"]

        # session bindings: a workspace-less binding (workspace_id=None) must upsert in
        # place. SQLite treats NULL as distinct in a UNIQUE index, so without the IS NULL
        # upsert path each run would insert a duplicate row and find_session_binding could
        # return a stale session. Assert one row survives and the newest session wins.
        worker = db.upsert_worker({"base_url": "http://w1.local", "name": "w1"})
        conversation = db.create_conversation({"title": "binding"})
        db.upsert_session_binding(conversation["id"], worker["id"], None, "sess-old")
        db.upsert_session_binding(conversation["id"], worker["id"], None, "sess-new")
        with db.connect() as conn:
            binding_rows = conn.execute(
                "SELECT thclaws_session_id FROM session_bindings WHERE conversation_id = ?",
                (conversation["id"],),
            ).fetchall()
        assert len(binding_rows) == 1, f"workspace-less binding must upsert, got {len(binding_rows)} rows"
        assert db.find_session_binding(conversation["id"])["thclaws_session_id"] == "sess-new"

        # worker deletion must not destroy job history (jobs FK is ON DELETE CASCADE): a
        # worker with jobs cannot be deleted; a worker with none deletes normally.
        db.create_job({"worker_id": worker["id"], "prompt": "p", "state": "succeeded"})
        try:
            db.delete_worker(worker["id"])
        except ValueError as exc:
            assert "history" in str(exc), exc
        else:
            raise AssertionError("deleting a worker with job history must be blocked")
        assert db.get_worker(worker["id"]) is not None, "blocked delete must leave the worker"
        spare = db.upsert_worker({"base_url": "http://spare.local", "name": "spare"})
        assert db.delete_worker(spare["id"]) is True, "a worker with no jobs must delete"

        assert db.delete_workflow_definition(definition["id"])
        assert db.get_workflow_definition(definition["id"]) is None

    print("workflow db check ok")


if __name__ == "__main__":
    main()
