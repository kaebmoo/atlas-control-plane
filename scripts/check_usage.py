from __future__ import annotations

import csv
import io
import json
import sys
import threading
import time
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
    normalize_usage_range,
    summarize_usage,
    usage_threshold_alert,
    verify_signed_usage_export_file,
    write_signed_usage_export,
)
from scripts.check_lib import request, request_json


class MockThClawsHandler(BaseHTTPRequestHandler):
    output_rate = 4.0

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:
        assert self.path == "/agent/run"
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = (
            b"event: text\ndata: metered result\n\n"
            b'event: usage\ndata: {"model": "priced-model", "prompt_tokens": 120, "completion_tokens": 45,'
            b' "cached_input_tokens": 10, "cache_creation_input_tokens": 5,'
            b' "reasoning_output_tokens": 7}\n\n'
            # A later PARTIAL usage frame updates its own key only — it must never
            # clobber the prompt/completion counts already seen back to NULL.
            b'event: usage\ndata: {"reasoning_output_tokens": 9}\n\n'
            b'event: result\ndata: {"stop_reason": "stop"}\n\n'
            b"event: done\ndata: [DONE]\n\n"
        )
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            payload = {"ok": True}
        elif self.path == "/v1/agent/info":
            payload = {"version": "0.85.0"}
        elif self.path == "/v1/models":
            payload = {
                "object": "list",
                "data": [
                    {
                        "id": "priced-model",
                        "pricing": {
                            "currency": "USD",
                            "input_per_mtok": 2.0,
                            "output_per_mtok": self.output_rate,
                            "cached_input_per_mtok": 0.2,
                            "cache_creation_per_mtok": 3.0,
                            "reasoning_per_mtok": 5.0,
                        },
                    },
                    {
                        "id": "partial-model",
                        "pricing": {"currency": "USD", "input_per_mtok": 2.0},
                    },
                    {
                        "id": "overflow-model",
                        "pricing": {"currency": "USD", "input_per_mtok": 10**400},
                    },
                    {
                        "id": "infinity-model",
                        "pricing": {"currency": "USD", "input_per_mtok": 1e308},
                    },
                ],
            }
        else:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = json.dumps(payload).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
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
        runtime.jobs.poll_worker(worker["id"])
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
            # T1a: tokens parsed from the worker's `usage` SSE event, full payload in measures.
            assert job_event["tokens_prompt"] == 120 and job_event["tokens_output"] == 45
            measures = job_event["metadata"]["measures"]
            assert measures["prompt_tokens"] == 120 and measures["completion_tokens"] == 45
            assert measures["cached_input_tokens"] == 10
            assert measures["cache_creation_input_tokens"] == 5
            # 9, not 7: the partial second frame updated this key (per-key last-seen merge)
            # without clobbering prompt/completion above.
            assert measures["reasoning_output_tokens"] == 9
            assert job_event["metadata"]["byok_token_counts_billable"] is False
            # T1b: worker-reported model wins over the requested model and the exact pricing
            # block used for this estimate is frozen into the event.
            assert job_event["model"] == "priced-model"
            assert job_event["metadata"]["effective_model_source"] == "worker"
            assert job_event["metadata"]["pricing_snapshot"]["output_per_mtok"] == 4.0
            assert job_event["metadata"]["estimated_cost_usd"] == 0.000462
            assert job_event["metadata"]["estimate"] is True
            assert job_event["metadata"]["pricing_partial"] is False
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
            assert totals["tokens_prompt"] == 120 and totals["tokens_output"] == 45
            assert totals["estimated_cost_usd"] == 0.000462

            # A later catalogue refresh changes the live rate but MUST NOT re-price history:
            # summarize_usage reads only the event snapshot above.
            MockThClawsHandler.output_rate = 400.0
            runtime.jobs.poll_worker(worker["id"])
            assert summarize_usage(runtime.db.list_usage_events())["estimated_cost_usd"] == 0.000462

            job = runtime.db.get_job(job_event["job_id"])
            unknown = runtime.jobs._usage_payload(job, measures, job["finished_at"], "unknown-model")
            assert unknown["tokens_prompt"] == 120 and unknown["tokens_output"] == 45
            assert "pricing_snapshot" not in unknown["metadata"]
            assert "estimated_cost_usd" not in unknown["metadata"]
            partial = runtime.jobs._usage_payload(job, measures, job["finished_at"], "partial-model")
            assert partial["metadata"]["estimated_cost_usd"] == 0.00024
            assert partial["metadata"]["pricing_partial"] is True
            # Semi-trusted catalogue numbers must never overflow into Infinity or suppress
            # the usage row. They remain an immutable raw snapshot but produce no estimate.
            overflow = runtime.jobs._usage_payload(job, measures, job["finished_at"], "overflow-model")
            assert overflow["tokens_prompt"] == 120
            assert "estimated_cost_usd" not in overflow["metadata"], overflow
            huge_usage = {"prompt_tokens": 2**63 - 1}
            infinity = runtime.jobs._usage_payload(job, huge_usage, job["finished_at"], "infinity-model")
            assert infinity["tokens_prompt"] == 2**63 - 1
            assert "estimated_cost_usd" not in infinity["metadata"], infinity

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

            # Range/precision contract — stored created_at is second-resolution, and boundaries
            # are snapped to whole seconds in their inclusive direction, so for both audit and
            # usage:
            #   * an inclusive `to` equal to an event's own timestamp keeps that event;
            #   * a `from` even ONE MICROSECOND after it drops the event that precedes the
            #     boundary. The +1us case is the teeth: a julianday()/float comparator collapses
            #     sub-millisecond deltas and would wrongly keep the row (the bug this locks).
            def assert_boundaries(name, ts, list_fn, row_id):
                _, to_exact = normalize_usage_range(None, ts)
                assert any(r["id"] == row_id for r in list_fn(to_at=to_exact)), f"{name} to_exact {to_exact}"
                from_exact, _ = normalize_usage_range(ts, None)
                assert any(r["id"] == row_id for r in list_fn(from_at=from_exact)), f"{name} from_exact {from_exact}"
                for delta in (".000001Z", ".000500Z", ".900000Z"):  # +1us, +0.5ms, +0.9s
                    from_after, _ = normalize_usage_range(ts[:-1] + delta, None)
                    assert all(r["id"] != row_id for r in list_fn(from_at=from_after)), f"{name} from{delta} {from_after}"

            assert_boundaries("usage", run_event["created_at"], runtime.db.list_usage_events, run_event["id"])
            newest_audit = runtime.db.list_audit(50)[0]
            assert_boundaries("audit", newest_audit["created_at"], runtime.db.list_audit, newest_audit["id"])

            # A valid sub-second-wide window (from < to, both inside one second) must be accepted
            # and return zero rows — NOT rejected as reversed after snapping inverts it.
            second = run_event["created_at"][:-1]
            narrow_from, narrow_to = normalize_usage_range(second + ".100000Z", second + ".900000Z")
            assert runtime.db.list_usage_events(from_at=narrow_from, to_at=narrow_to) == [], (narrow_from, narrow_to)
            assert runtime.db.list_audit(from_at=narrow_from, to_at=narrow_to) == [], (narrow_from, narrow_to)
            # A genuinely reversed raw range is still a 400 (ValueError), decided pre-snap.
            try:
                normalize_usage_range("2100-01-01T00:00:00Z", "2000-01-01T00:00:00Z")
            except ValueError:
                pass
            else:
                raise AssertionError("reversed range must raise ValueError")
            # Ceil at the datetime maximum must surface as a 400 (ValueError), never an HTTP 500.
            try:
                normalize_usage_range("9999-12-31T23:59:59.000001Z", None)
            except ValueError:
                pass
            except OverflowError:
                raise AssertionError("ceil overflow escaped as OverflowError (HTTP 500) instead of ValueError")
            else:
                raise AssertionError("ceil overflow at datetime max must raise ValueError (HTTP 400)")

            # Extreme-but-valid timezone offsets overflow while converting to UTC, BEFORE the snap
            # guard — both datetime edges, in either slot, must be a 400 (ValueError), never a 500.
            for edge in ("0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00"):
                for from_at, to_at in ((edge, None), (None, edge)):
                    try:
                        normalize_usage_range(from_at, to_at)
                    except ValueError:
                        pass
                    except OverflowError:
                        raise AssertionError(f"offset overflow escaped as OverflowError (HTTP 500): {edge}")
                    else:
                        raise AssertionError(f"offset overflow must raise ValueError (HTTP 400): {edge}")

            # metrics usage_units is the workflow-run budget total (3), not job(1)+run(3) mixed (4).
            snapshot = runtime.db.metrics_snapshot()
            assert snapshot["usage_units"] == 3 and snapshot["usage_events"] == 2, snapshot

            status, usage_json, _ = request_json(base_url, "GET", "/api/usage?format=json", token=tokens["admin"])
            assert status == 200 and usage_json["totals"]["workflow_runs"] == 1
            assert usage_json["totals"]["tokens_prompt"] == 120 and usage_json["totals"]["tokens_output"] == 45
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
