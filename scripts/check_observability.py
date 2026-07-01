"""Cross-cutting observability/compliance check (sovereign-platform-plan.md, cross-cutting).

Covers, hermetically (own temp DB, ephemeral port, no external workers):
  1. GET /api/metrics — aggregate counters, read RBAC (401 without a token).
  2. GET /api/audit — additive from/to + format=csv export; bad format -> 400.
  3. Artifact data-classification tag — validated at the db create path; bad tag -> 400.
  4. Retention purge — dry-run reports without deleting; real purge removes rows AND
     file_ref bytes, never touches a non-terminal run's artifacts, and writes an audit entry.
  5. `python3 -m atlas.admin purge-artifacts` CLI wraps the same purge.
"""

from __future__ import annotations

import csv
import io
import json
import sys
import threading
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas import admin
from atlas.app import AtlasHttpServer, AtlasRuntime
from atlas.config import Config
from scripts.check_lib import request, request_json

JOIN_ONLY_GRAPH = {"start": "done", "nodes": [{"id": "done", "type": "join", "mode": "all"}], "edges": []}
GATE_GRAPH = {
    "start": "gate",
    "nodes": [{"id": "gate", "type": "human_gate", "label": "Hold"}, {"id": "done", "type": "join", "mode": "all"}],
    "edges": [{"from": "gate", "to": "done", "condition": {"type": "always"}}],
}


def main() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        runtime = AtlasRuntime(
            Config(
                host="127.0.0.1",
                port=0,
                db_path=root / "atlas.sqlite",
                api_token=None,
                request_timeout_seconds=2,
                enable_loopback_without_token=False,
                secret_key="observability-secret",
                upload_dir=root / "uploads",
            )
        )
        tokens = {}
        for role in ("admin", "auditor", "viewer", "operator"):
            user = runtime.db.create_user(role, f"{role}-password", role)
            _, tokens[role] = runtime.db.create_api_token(user["id"], f"{role} observability check")

        server = AtlasHttpServer(("127.0.0.1", 0), runtime)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            definition = runtime.db.create_workflow_definition({"name": "Obs join", "graph": JOIN_ONLY_GRAPH})
            status, started, _ = request_json(
                base_url, "POST", "/api/workflow-runs",
                {"workflow_definition_id": definition["id"]}, tokens["operator"],
            )
            assert status == 202, started
            run = wait_for_run(runtime, started["run"]["id"], "succeeded")

            # 1) metrics: 401 unauthenticated; viewer (read) sees aggregate counters.
            assert request(base_url, "GET", "/api/metrics")[0] == 401
            status, payload, _ = request_json(base_url, "GET", "/api/metrics", token=tokens["viewer"])
            assert status == 200, payload
            metrics = payload["metrics"]
            assert metrics["workflow_runs"].get("succeeded") == 1, metrics
            assert metrics["workflow_definitions"] == 1 and metrics["schema_version"] >= 1, metrics
            assert metrics["approvals_pending"] == 0 and "version" in metrics and "time" in metrics

            # 2) artifact classification: valid tag lands in metadata; invalid -> 400.
            status, created, _ = request_json(
                base_url, "POST", "/api/artifacts",
                {"run_id": run["id"], "key": "report", "kind": "text", "content": "x", "classification": "confidential"},
                tokens["operator"],
            )
            assert status == 201 and created["artifact"]["metadata"]["classification"] == "confidential", created
            text_artifact_id = created["artifact"]["id"]
            status, error, _ = request_json(
                base_url, "POST", "/api/artifacts",
                {"run_id": run["id"], "key": "bad", "kind": "text", "content": "x", "classification": "top-secret"},
                tokens["operator"],
            )
            assert status == 400 and "classification" in error["error"], error

            # file_ref artifact for byte-purge coverage.
            file_request = urllib.request.Request(
                f"{base_url}/api/workflow-runs/{run['id']}/files?key=evidence",
                data=b"file-bytes",
                method="POST",
                headers={
                    "Authorization": f"Bearer {tokens['operator']}",
                    "Content-Type": "application/octet-stream",
                    "X-Filename": "evidence.bin",
                },
            )
            with urllib.request.urlopen(file_request, timeout=5) as response:
                file_artifact = json.loads(response.read())["artifact"]
            file_on_disk = runtime.upload_dir / file_artifact["content"]
            assert file_on_disk.is_file()

            # 3) audit export: csv parses and carries the artifact.create rows; bad format -> 400.
            status, body, _ = request(base_url, "GET", "/api/audit?format=csv", token=tokens["auditor"])
            assert status == 200
            rows = list(csv.DictReader(io.StringIO(body.decode("utf-8"))))
            assert any(row["action"] == "artifact.create" for row in rows), rows[:3]
            assert request(base_url, "GET", "/api/audit?format=xml", token=tokens["auditor"])[0] == 400
            future = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
            status, payload, _ = request_json(base_url, "GET", f"/api/audit?from={future}", token=tokens["auditor"])
            assert status == 200 and payload["audit"] == [], payload

            # 4) purge: a non-terminal run's artifact survives; terminal-run artifacts go.
            gate_definition = runtime.db.create_workflow_definition({"name": "Obs gate", "graph": GATE_GRAPH})
            status, gated, _ = request_json(
                base_url, "POST", "/api/workflow-runs",
                {"workflow_definition_id": gate_definition["id"]}, tokens["operator"],
            )
            assert status == 202, gated
            wait_for_run(runtime, gated["run"]["id"], "waiting_for_human")
            live_artifact = runtime.db.create_artifact({"run_id": gated["run"]["id"], "key": "live", "kind": "text", "content": "keep"})

            backdated = "2000-01-01T00:00:00Z"
            with runtime.db._lock, runtime.db.connect() as conn:  # test-only backdate
                conn.execute("UPDATE artifacts SET created_at = ?", (backdated,))

            cutoff = "2001-01-01T00:00:00Z"
            preview = runtime.db.purge_artifacts(cutoff, upload_dir=runtime.upload_dir, dry_run=True)
            assert preview["dry_run"] and preview["purged"] == 2, preview
            assert runtime.db.get_artifact(text_artifact_id) and file_on_disk.is_file()

            result = runtime.db.purge_artifacts(cutoff, upload_dir=runtime.upload_dir, dry_run=False)
            assert result["purged"] == 2 and result["files_deleted"] == 1, result
            assert runtime.db.get_artifact(text_artifact_id) is None
            assert runtime.db.get_artifact(file_artifact["id"]) is None
            assert not file_on_disk.exists()
            assert runtime.db.get_artifact(live_artifact["id"]), "non-terminal run artifact must survive purge"
            assert any(entry["action"] == "artifact.purge" for entry in runtime.db.list_audit(50))

            # 5) admin CLI wraps the same purge (dry run keeps the survivor untouched).
            cli_env = {"ATLAS_DB": str(runtime.config.db_path), "ATLAS_SECRET_KEY": "observability-secret"}
            with mock.patch.dict("os.environ", cli_env), mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                admin.main(["purge-artifacts", "--older-than-days", "1", "--dry-run"])
            cli_result = json.loads(stdout.getvalue())
            assert cli_result["dry_run"] is True and cli_result["purged"] == 0, cli_result
        finally:
            server.shutdown()

    print("observability check ok")


def wait_for_run(runtime: AtlasRuntime, run_id: str, state: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        run = runtime.db.get_workflow_run(run_id)
        if run and run["state"] == state:
            return run
        time.sleep(0.02)
    raise AssertionError(f"run never reached {state}: {runtime.db.get_workflow_run(run_id)}")


if __name__ == "__main__":
    main()
