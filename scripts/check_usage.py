from __future__ import annotations

import csv
import io
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.app import AtlasHttpServer, AtlasRuntime
from atlas.config import Config
from atlas.usage import (
    summarize_usage,
    usage_threshold_alert,
    verify_signed_usage_export_file,
    write_signed_usage_export,
)


class MockThClawsHandler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:
        assert self.path == "/agent/run"
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = b"event: text\ndata: metered result\n\nevent: done\ndata: [DONE]\n\n"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        mock_worker = ThreadingHTTPServer(("127.0.0.1", 0), MockThClawsHandler)
        mock_worker_thread = threading.Thread(target=mock_worker.serve_forever, daemon=True)
        mock_worker_thread.start()

        runtime = AtlasRuntime(
            Config(
                host="127.0.0.1",
                port=0,
                db_path=root / "atlas.sqlite",
                api_token=None,
                request_timeout_seconds=2,
                enable_loopback_without_token=False,
                secret_key="usage-signing-secret",
                upload_dir=root / "uploads",
            )
        )
        tokens = create_role_tokens(runtime)
        worker = runtime.db.upsert_worker(
            {"name": "Mock usage worker", "base_url": f"http://127.0.0.1:{mock_worker.server_address[1]}"}
        )
        definition = runtime.db.create_workflow_definition(
            {
                "name": "Metered workflow",
                "graph": {
                    "start": "work",
                    "nodes": [
                        {
                            "id": "work",
                            "type": "worker",
                            "worker_id": worker["id"],
                            "prompt": "meter this",
                            "model": "byok-visibility-model",
                            "budget_units": 3,
                        }
                    ],
                    "edges": [],
                },
                "policy": {"max_budget_units": 3},
            }
        )

        server = AtlasHttpServer(("127.0.0.1", 0), runtime)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            assert request(base_url, "GET", "/api/usage")[0] == 401
            assert request(base_url, "GET", "/api/usage", token=tokens["viewer"])[0] == 403
            assert request(base_url, "GET", "/api/usage", token=tokens["operator"])[0] == 403

            status, started, _ = request_json(
                base_url,
                "POST",
                "/api/workflow-runs",
                {"workflow_definition_id": definition["id"]},
                tokens["admin"],
            )
            assert status == 202
            run = wait_for_run(runtime, started["run"]["id"])
            wait_for_usage(runtime, 2)

            events = runtime.db.list_usage_events()
            assert len(events) == 2
            assert [event["kind"] for event in events].count("job") == 1
            assert [event["kind"] for event in events].count("workflow_run") == 1
            job_event = next(event for event in events if event["kind"] == "job")
            run_event = next(event for event in events if event["kind"] == "workflow_run")
            assert job_event["idempotency_key"] == f"job:{job_event['job_id']}" and job_event["units"] == 1
            assert job_event["run_id"] == run["id"] and job_event["node_key"] == "work"
            assert job_event["model"] == "byok-visibility-model"
            assert job_event["tokens_prompt"] is None and job_event["tokens_output"] is None
            assert job_event["metadata"]["byok_token_counts_billable"] is False
            assert run_event["idempotency_key"] == f"run:{run['id']}"
            assert run_event["units"] == run["counters"]["budget_units_spent"] == 3
            assert run_event["metadata"]["measures"]["job_count"] == run["counters"]["jobs_started"] == 1
            assert run_event["metadata"]["billing_unit"] == "workflow_run"
            assert run_event["metadata"]["billable"] is True
            assert {event["actor"] for event in events} == {"admin"}

            totals = summarize_usage(events)
            assert totals["workflow_runs"] == 1
            assert totals["successful_workflow_runs"] == 1
            assert totals["jobs"] == run["counters"]["jobs_started"]
            assert totals["budget_units"] == run["counters"]["budget_units_spent"]

            runtime.jobs._record_job_usage(job_event["job_id"])
            runtime.workflows._record_workflow_usage(run["id"])
            assert len(runtime.db.list_usage_events()) == 2

            # B4: read-only run-count threshold alert; the Usage view reads the same data.
            ledger = runtime.db.list_usage_events()
            assert summarize_usage(ledger)["workflow_runs"] == 1
            crossed = usage_threshold_alert(ledger, expected_runs=1)
            assert crossed["used_runs"] == 1 and crossed["alert"] is True, crossed
            below = usage_threshold_alert(ledger, expected_runs=10)
            assert below["used_runs"] == 1 and below["alert"] is False, below
            # alert fires once volume crosses the configured threshold ratio
            assert usage_threshold_alert(ledger, expected_runs=2, threshold_ratio=0.4)["alert"] is True
            assert usage_threshold_alert(ledger, expected_runs=0)["alert"] is False
            # the volume alert never touches budget_units (the per-run cost guard)
            assert "budget_units" not in crossed
            assert summarize_usage(ledger)["budget_units"] == 3

            status, usage_json, _ = request_json(base_url, "GET", "/api/usage?format=json", token=tokens["admin"])
            assert status == 200 and usage_json["totals"]["workflow_runs"] == 1
            assert len(usage_json["usage"]) == 2
            ranged = request_json(
                base_url, "GET", "/api/usage?from=2000-01-01&to=2100-01-01", token=tokens["admin"]
            )[1]
            assert len(ranged["usage"]) == 2
            future = request_json(base_url, "GET", "/api/usage?from=2100-01-01", token=tokens["admin"])[1]
            assert future["usage"] == [] and future["totals"]["workflow_runs"] == 0
            assert request(base_url, "GET", "/api/usage?format=json", token=tokens["auditor"])[0] == 200
            csv_status, csv_body, csv_headers = request(base_url, "GET", "/api/usage?format=csv", token=tokens["auditor"])
            assert csv_status == 200 and csv_headers["Content-Type"].startswith("text/csv")
            rows = list(csv.DictReader(io.StringIO(csv_body.decode("utf-8"))))
            assert len(rows) == 2 and {row["kind"] for row in rows} == {"job", "workflow_run"}
            assert json.loads(next(row for row in rows if row["kind"] == "workflow_run")["metadata"])["billing_unit"] == "workflow_run"

            export_path = root / "offline-usage.json"
            exported = write_signed_usage_export(runtime.db, export_path, "usage-signing-secret")
            assert exported["payload"]["totals"]["budget_units"] == 3
            assert verify_signed_usage_export_file(export_path, "usage-signing-secret")
            tampered = json.loads(export_path.read_text(encoding="utf-8"))
            tampered["payload"]["usage"][0]["units"] += 1
            export_path.write_text(json.dumps(tampered), encoding="utf-8")
            assert not verify_signed_usage_export_file(export_path, "usage-signing-secret")

            check_metering_failure_is_non_fatal(runtime, base_url, definition["id"], tokens["admin"])
            assert len(runtime.db.list_usage_events()) == 2
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)
            mock_worker.shutdown()
            mock_worker.server_close()
            mock_worker_thread.join(timeout=2)

    print("usage check ok")


def create_role_tokens(runtime: AtlasRuntime) -> dict[str, str]:
    tokens = {}
    for role in ("admin", "auditor", "viewer", "operator"):
        user = runtime.db.create_user(role, f"{role}-password", role)
        _, tokens[role] = runtime.db.create_api_token(user["id"], f"{role} usage check")
    return tokens


def check_metering_failure_is_non_fatal(runtime: AtlasRuntime, base_url: str, definition_id: str, token: str) -> None:
    original = runtime.db.emit_usage_event

    def fail_metering(_payload: dict) -> dict:
        raise RuntimeError("simulated metering outage")

    runtime.db.emit_usage_event = fail_metering
    try:
        with mock.patch("atlas.jobs.LOGGER.exception") as job_log, mock.patch("atlas.workflows.LOGGER.exception") as run_log:
            status, payload, _ = request_json(
                base_url,
                "POST",
                "/api/workflow-runs",
                {"workflow_definition_id": definition_id},
                token,
            )
            assert status == 202
            run = wait_for_run(runtime, payload["run"]["id"])
            wait_for_background_threads(runtime, run["id"])
            assert run["state"] == "succeeded"
            assert job_log.called and run_log.called
    finally:
        runtime.db.emit_usage_event = original


def request(
    base_url: str,
    method: str,
    path: str,
    payload: dict | None = None,
    token: str | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base_url + path, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def request_json(
    base_url: str,
    method: str,
    path: str,
    payload: dict | None = None,
    token: str | None = None,
) -> tuple[int, dict, dict[str, str]]:
    status, body, headers = request(base_url, method, path, payload, token)
    return status, json.loads(body or b"{}"), headers


def wait_for_run(runtime: AtlasRuntime, run_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        run = runtime.db.get_workflow_run(run_id)
        if run and run["state"] in {"succeeded", "failed", "cancelled"}:
            assert run["state"] == "succeeded", run
            return run
        time.sleep(0.02)
    raise AssertionError(f"workflow did not finish: {runtime.db.get_workflow_run(run_id)}")


def wait_for_usage(runtime: AtlasRuntime, count: int) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if len(runtime.db.list_usage_events()) == count:
            return
        time.sleep(0.01)
    raise AssertionError(f"usage event count did not reach {count}: {runtime.db.list_usage_events()}")


def wait_for_background_threads(runtime: AtlasRuntime, run_id: str) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if run_id not in runtime.workflows._threads and not runtime.jobs._threads:
            return
        time.sleep(0.01)
    raise AssertionError("usage failure check threads did not stop")


if __name__ == "__main__":
    main()
