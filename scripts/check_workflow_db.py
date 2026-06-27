from __future__ import annotations

import sys
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
        assert {
            "workflow_definitions",
            "workflow_runs",
            "workflow_nodes",
            "workflow_edges",
            "workflow_events",
            "artifacts",
            "workflow_triggers",
            "workflow_trigger_events",
        } <= tables

        definition = db.create_workflow_definition(
            {
                "name": "Smoke",
                "graph": {"start": "a", "nodes": [], "edges": []},
                "policy": {"max_jobs": 1},
            }
        )
        assert definition["graph"]["start"] == "a"
        assert db.list_workflow_definitions()[0]["id"] == definition["id"]
        assert db.update_workflow_definition(definition["id"], {"status": "active"})["status"] == "active"

        run = db.create_workflow_run({"workflow_definition_id": definition["id"], "input": {"topic": "x"}})
        assert db.get_workflow_run(run["id"])["input"]["topic"] == "x"
        assert db.list_workflow_runs(workflow_definition_id=definition["id"])[0]["id"] == run["id"]
        event = db.append_workflow_event(run["id"], "node_started", {"attempt": 1}, node_key="a")
        events = db.list_workflow_events(run["id"])
        assert [item["seq"] for item in events] == [1, 2]
        assert [item["event_type"] for item in events] == ["created", "node_started"]
        assert event["node_key"] == "a"

        trigger = db.create_workflow_trigger({"workflow_definition_id": definition["id"], "name": "Manual", "type": "manual"})
        event = db.append_workflow_trigger_event(trigger["id"], "received", {"topic": "x"}, run_id=run["id"], dedupe_key="one")
        assert db.list_workflow_trigger_events(trigger["id"])[0]["id"] == event["id"]

        artifact = db.create_artifact({"run_id": run["id"], "key": "notes", "content": "ok"})
        assert db.list_artifacts(run_id=run["id"])[0]["id"] == artifact["id"]

        assert db.delete_workflow_definition(definition["id"])
        assert db.get_workflow_definition(definition["id"]) is None

    print("workflow db check ok")


if __name__ == "__main__":
    main()
