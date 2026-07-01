"""IA-1 hermetic check (docs/plans/input-adapter-return-path-plan.md).

Verifies the Input Adapter Contract (docs/specs/input-adapter-contract.md): the reserved
`_meta` envelope is parsed/validated on BOTH ingress paths (`/api/workflow-triggers/{id}/fire`
and `POST /api/workflow-runs`), a legacy payload without `_meta` is unaffected, `_meta.source`
is recorded in the audit log against the run_id, an invalid envelope is rejected pre-run with
no run created, and `_meta.reply` round-trips through the persisted run input.
"""

from __future__ import annotations

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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.app import AtlasHttpServer, AtlasRuntime
from atlas.config import Config


class MockThClawsHandler(BaseHTTPRequestHandler):
    received_prompts: list[str] = []

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:
        assert self.path == "/agent/run"
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = json.loads(self.rfile.read(length) or b"{}")
        MockThClawsHandler.received_prompts.append(body.get("prompt", ""))
        response_body = b"event: text\ndata: received\n\nevent: done\ndata: [DONE]\n\n"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)


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
                enable_loopback_without_token=True,
                upload_dir=root / "uploads",
                # An IP literal allowlist entry matches by exact string AND resolves instantly
                # (no real DNS lookup for a literal), so this stays hermetic — see
                # atlas/outbound.py resolve_outbound_target.
                outbound_allowlist=("127.0.0.1",),
            )
        )
        worker = runtime.db.upsert_worker(
            {"name": "Mock IA worker", "base_url": f"http://127.0.0.1:{mock_worker.server_address[1]}"}
        )
        definition = runtime.db.create_workflow_definition(
            {
                "name": "Complaint intake",
                "graph": {
                    "start": "work",
                    "nodes": [
                        {"id": "work", "type": "worker", "worker_id": worker["id"], "prompt": "Complaint: {input.complaint_text}"}
                    ],
                    "edges": [],
                },
                "policy": {"max_jobs": 1},
            }
        )
        trigger = runtime.db.create_workflow_trigger(
            {"workflow_definition_id": definition["id"], "name": "Webhook intake", "type": "webhook"}
        )

        server = AtlasHttpServer(("127.0.0.1", 0), runtime)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            run_count_before = len(runtime.db.list_workflow_runs(limit=1000))

            # 1. /fire with a full envelope: run created, provenance audited, a business field
            #    reaches the mock worker's prompt, and _meta.reply round-trips.
            fired = request(
                base_url,
                "POST",
                f"/api/workflow-triggers/{trigger['id']}/fire",
                {
                    "payload": {
                        "complaint_text": "ไฟถนนดับ",
                        "_meta": {
                            "source": {
                                "channel": "line",
                                "adapter": "n8n",
                                "form": "gov_complaint",
                                "external_id": "line-msg-1",
                            },
                            "reply": {
                                "mode": "webhook",
                                "callback_url": "https://127.0.0.1/atlas/reply",
                                "correlation_id": "line:U1:msg-1",
                            },
                        },
                    },
                    "dedupe_key": "line:line-msg-1",
                },
            )
            run_id = fired["run"]["id"]
            run = wait_for_run(runtime, run_id)
            assert "Complaint: ไฟถนนดับ" in MockThClawsHandler.received_prompts, MockThClawsHandler.received_prompts

            provenance = _provenance_entries(runtime, run_id)
            assert len(provenance) == 1, provenance
            assert provenance[0]["details"] == {
                "channel": "line",
                "adapter": "n8n",
                "form": "gov_complaint",
                "external_id": "line-msg-1",
            }, provenance

            persisted_reply = run["input"]["_meta"]["reply"]
            assert persisted_reply["correlation_id"] == "line:U1:msg-1", persisted_reply
            assert persisted_reply["callback_url"] == "https://127.0.0.1/atlas/reply", persisted_reply

            # 2. /fire WITHOUT _meta (legacy payload): unaffected end-to-end, no provenance entry.
            legacy = request(
                base_url,
                "POST",
                f"/api/workflow-triggers/{trigger['id']}/fire",
                {"payload": {"complaint_text": "legacy, no meta"}, "dedupe_key": "legacy-1"},
            )
            legacy_run_id = legacy["run"]["id"]
            wait_for_run(runtime, legacy_run_id)
            assert "Complaint: legacy, no meta" in MockThClawsHandler.received_prompts
            assert not _provenance_entries(runtime, legacy_run_id)

            # 3. POST /api/workflow-runs directly with _meta in input: same provenance recorded.
            direct = request(
                base_url,
                "POST",
                "/api/workflow-runs",
                {
                    "workflow_definition_id": definition["id"],
                    "input": {
                        "complaint_text": "web form complaint",
                        "_meta": {"source": {"channel": "web_form", "form": "service_request", "external_id": "req-1"}},
                    },
                },
            )
            direct_run_id = direct["run"]["id"]
            wait_for_run(runtime, direct_run_id)
            direct_provenance = _provenance_entries(runtime, direct_run_id)
            assert len(direct_provenance) == 1, direct_provenance
            assert direct_provenance[0]["details"]["channel"] == "web_form", direct_provenance

            # 4. Invalid envelopes are rejected pre-run (400, no run created) on the direct path.
            bad_shape = request_error(
                base_url,
                "POST",
                "/api/workflow-runs",
                {"workflow_definition_id": definition["id"], "input": {"_meta": "oops"}},
            )
            assert "_meta must be an object" in bad_shape["error"], bad_shape

            bad_channel = request_error(
                base_url,
                "POST",
                "/api/workflow-runs",
                {"workflow_definition_id": definition["id"], "input": {"_meta": {"source": {"channel": "sms"}}}},
            )
            assert "channel" in bad_channel["error"], bad_channel

            bad_callback = request_error(
                base_url,
                "POST",
                "/api/workflow-runs",
                {
                    "workflow_definition_id": definition["id"],
                    "input": {"_meta": {"reply": {"mode": "webhook", "callback_url": "https://10.1.2.3/reply"}}},
                },
            )
            assert "not deliverable" in bad_callback["error"], bad_callback

            # A callback_url embedding credentials (userinfo or a credential-shaped query key)
            # is rejected pre-run too — it must never be persisted into run input where any
            # "read"-permission role could later see it via GET /api/workflow-runs.
            bad_userinfo = request_error(
                base_url,
                "POST",
                "/api/workflow-runs",
                {
                    "workflow_definition_id": definition["id"],
                    "input": {"_meta": {"reply": {"mode": "webhook", "callback_url": "https://user:pass@relay.internal.test/reply"}}},
                },
            )
            assert "credentials" in bad_userinfo["error"], bad_userinfo

            bad_query_secret = request_error(
                base_url,
                "POST",
                "/api/workflow-runs",
                {
                    "workflow_definition_id": definition["id"],
                    "input": {
                        "_meta": {"reply": {"mode": "webhook", "callback_url": "https://127.0.0.1/reply?access_token=TOPSECRET"}}
                    },
                },
            )
            assert "credentials" in bad_query_secret["error"], bad_query_secret

            # Same invalid envelope via /fire: always 202 (fire never surfaces a 4xx), but no run
            # starts — the trigger event records the rejection instead.
            fired_bad = request(
                base_url,
                "POST",
                f"/api/workflow-triggers/{trigger['id']}/fire",
                {"payload": {"_meta": {"source": {"channel": "carrier-pigeon"}}}, "dedupe_key": "bad-1"},
            )
            assert fired_bad["run"] is None, fired_bad
            assert fired_bad["event"]["state"] == "failed", fired_bad
            assert "channel" in fired_bad["event"]["error"], fired_bad

            # Exactly the 3 valid attempts started a run; the 6 invalid attempts created none.
            assert len(runtime.db.list_workflow_runs(limit=1000)) == run_count_before + 3
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)
            mock_worker.shutdown()
            mock_worker.server_close()
            mock_worker_thread.join(timeout=2)

    print("input adapter check ok")


def _provenance_entries(runtime: AtlasRuntime, run_id: str) -> list[dict]:
    return [
        entry
        for entry in runtime.db.list_audit(limit=1000)
        if entry["action"] == "workflow_run.provenance" and entry["resource_id"] == run_id
    ]


def request(base_url: str, method: str, path: str, payload: dict | None = None) -> dict:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base_url + path, data=body, method=method, headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def request_error(base_url: str, method: str, path: str, payload: dict | None = None) -> dict:
    try:
        return request(base_url, method, path, payload)
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read().decode("utf-8"))
    raise AssertionError("expected HTTPError")


def wait_for_run(runtime: AtlasRuntime, run_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        run = runtime.db.get_workflow_run(run_id)
        if run and run["state"] in {"succeeded", "failed", "cancelled"}:
            assert run["state"] == "succeeded", run
            return run
        time.sleep(0.02)
    raise AssertionError(f"workflow run {run_id} did not finish: {runtime.db.get_workflow_run(run_id)}")


if __name__ == "__main__":
    main()
