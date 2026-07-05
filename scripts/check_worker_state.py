"""T4 — advisory worker state + info surface. Hermetic checks (own temp DB, ephemeral port,
mock thClaws worker) for the sync_mode gate, the busy probe, and the advisory router signals.

Mutation targets (break the code -> this file goes red):
- store sync_mode inside agent_info instead of its own column -> the poll erases it.
- skip the enable-time sync_stat probe in app.py -> an unreachable worker gets enabled.
- invert the router busy tie-break (prefer busy) -> the busy worker wins.
- probe /workspace/sync/stat even when sync_mode == 'disabled' -> stat_hits > 0.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.app import AtlasHttpServer, AtlasRuntime
from atlas.config import Config
from atlas.db import WORKER_SYNC_MODES
from scripts.check_lib import request, request_json


class MockWorker(BaseHTTPRequestHandler):
    # Test-controlled behaviour (class attributes so a test can flip them between polls).
    stat_status = 200
    stat_busy: object = False
    version = "0.85.0"
    skills: list = []
    stat_hits = 0

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        cls = type(self)
        if self.path == "/healthz":
            payload: object = {"ok": True}
        elif self.path == "/v1/agent/info":
            payload = {"version": cls.version, "skills": cls.skills}
        elif self.path == "/v1/models":
            payload = {"object": "list", "data": []}
        elif self.path == "/workspace/sync/stat":
            cls.stat_hits += 1
            if cls.stat_status != 200:
                self.send_error(cls.stat_status)
                return
            payload = {"busy": cls.stat_busy}
        else:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = json.dumps(payload).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _online(runtime: AtlasRuntime, worker_id: str, agent_info: dict) -> None:
    """Force a worker online with a chosen agent_info blob (the poll-owned home of busy/skills)."""
    runtime.db.update_worker_status(worker_id, "online", agent_info)


def check_migration_and_default(runtime: AtlasRuntime, base_url_mock: str) -> None:
    assert runtime.db.schema_version() >= 9, runtime.db.schema_version()
    worker = runtime.db.upsert_worker({"name": "default-mode", "base_url": base_url_mock})
    assert worker["sync_mode"] == "disabled", worker.get("sync_mode")
    # Enum, not a bare flag, and validated.
    assert WORKER_SYNC_MODES == {"disabled", "tunnel", "forward_auth"}
    for bad in ("", "bearer", "on", None):
        try:
            runtime.db.set_worker_sync_mode(worker["id"], bad)  # type: ignore[arg-type]
            raise AssertionError(f"accepted invalid mode {bad!r}")
        except ValueError:
            pass
    # upsert_worker dedups by base_url; delete so the next poll-based test starts on a fresh row.
    runtime.db.delete_worker(worker["id"])


def check_sync_mode_survives_poll(runtime: AtlasRuntime, base_url_mock: str) -> None:
    worker = runtime.db.upsert_worker({"name": "persist-mode", "base_url": base_url_mock})
    runtime.db.set_worker_sync_mode(worker["id"], "tunnel")
    # The poll rewrites agent_info WHOLESALE. sync_mode lives in its own column, so it must
    # survive. (Mutation: store sync_mode in agent_info -> this poll erases it -> assert fails.)
    MockWorker.stat_status = 200
    MockWorker.stat_busy = False
    runtime.jobs.poll_worker(worker["id"])
    assert runtime.db.get_worker(worker["id"])["sync_mode"] == "tunnel"
    runtime.db.delete_worker(worker["id"])


def check_poll_busy_probe_is_gated(runtime: AtlasRuntime, base_url_mock: str) -> None:
    worker = runtime.db.upsert_worker({"name": "probe-gate", "base_url": base_url_mock})
    # disabled -> NO sync request at all, no busy field.
    MockWorker.stat_hits = 0
    runtime.jobs.poll_worker(worker["id"])
    info = runtime.db.get_worker(worker["id"])["agent_info"]
    assert MockWorker.stat_hits == 0, "disabled worker must not be probed"
    assert "busy" not in info, info

    # enabled + worker reports busy True -> stored.
    runtime.db.set_worker_sync_mode(worker["id"], "tunnel")
    MockWorker.stat_status = 200
    MockWorker.stat_busy = True
    MockWorker.stat_hits = 0
    runtime.jobs.poll_worker(worker["id"])
    info = runtime.db.get_worker(worker["id"])["agent_info"]
    assert MockWorker.stat_hits == 1, MockWorker.stat_hits
    assert info["busy"] is True and info.get("busy_checked_at"), info

    # probe error -> busy null ("unknown"), NEVER carried forward from the True above.
    MockWorker.stat_status = 404
    runtime.jobs.poll_worker(worker["id"])
    info = runtime.db.get_worker(worker["id"])["agent_info"]
    assert info["busy"] is None, info
    MockWorker.stat_status = 200

    # a non-bool busy from the worker -> null, never a truthy accident.
    MockWorker.stat_busy = "yes"
    runtime.jobs.poll_worker(worker["id"])
    assert runtime.db.get_worker(worker["id"])["agent_info"]["busy"] is None
    MockWorker.stat_busy = False
    runtime.db.delete_worker(worker["id"])


def check_router_busy_tiebreak(runtime: AtlasRuntime, base_url_mock: str) -> None:
    # Two equal-scored candidates (same tag, both online); busy breaks the tie.
    a = runtime.db.upsert_worker({"name": "aaa-tie", "base_url": base_url_mock + "/a", "tags": ["coder"]})
    b = runtime.db.upsert_worker({"name": "bbb-tie", "base_url": base_url_mock + "/b", "tags": ["coder"]})
    payload = {"tags": ["coder"], "allowed_worker_ids": [a["id"], b["id"]]}

    # Both busy unknown -> byte-identical to score-only: stable order (list_workers is name-sorted)
    # keeps the first-by-name worker, exactly as before T4.
    _online(runtime, a["id"], {"busy": None})
    _online(runtime, b["id"], {"busy": None})
    decision = runtime.router.resolve(payload)
    assert decision.worker["id"] == a["id"], "null busy must not change the winner"
    assert "advisory" not in decision.reason, decision.reason

    # aaa busy, bbb free -> the free worker wins the tie (Mutation: invert -> aaa wins -> red).
    _online(runtime, a["id"], {"busy": True})
    _online(runtime, b["id"], {"busy": False})
    decision = runtime.router.resolve(payload)
    assert decision.worker["id"] == b["id"], "not-busy worker must win the tie-break"
    assert "advisory: less busy" in decision.reason, decision.reason

    # busy worker never beats a strictly higher score (advisory is tie-break ONLY).
    runtime.db.upsert_worker({"id": b["id"], "base_url": base_url_mock + "/b", "tags": ["coder", "python"], "name": "bbb-tie"})
    _online(runtime, b["id"], {"busy": True})
    _online(runtime, a["id"], {"busy": False})
    decision = runtime.router.resolve({"tags": ["coder", "python"], "allowed_worker_ids": [a["id"], b["id"]]})
    assert decision.worker["id"] == b["id"], "higher score must win over a busy tie-break"
    runtime.db.delete_worker(a["id"])
    runtime.db.delete_worker(b["id"])


def check_fixture_stability(runtime: AtlasRuntime, base_url_mock: str) -> None:
    # A plain worker (no busy, no skills) routes with an UNCHANGED reason: no advisory text leaks.
    worker = runtime.db.upsert_worker({"name": "plain", "base_url": base_url_mock + "/plain", "role": "coder"})
    _online(runtime, worker["id"], {})
    decision = runtime.router.resolve({"role": "coder", "allowed_worker_ids": [worker["id"]]})
    assert decision.worker["id"] == worker["id"]
    assert "advisory: less busy" not in decision.reason and "skill hint" not in decision.reason, decision.reason
    runtime.db.delete_worker(worker["id"])


def check_skill_hint_is_advisory(runtime: AtlasRuntime, base_url_mock: str) -> None:
    a = runtime.db.upsert_worker({"name": "aaa-skill", "base_url": base_url_mock + "/sa", "tags": ["x"]})
    b = runtime.db.upsert_worker({"name": "bbb-skill", "base_url": base_url_mock + "/sb", "tags": ["x"]})
    _online(runtime, a["id"], {})
    _online(runtime, b["id"], {"agent": {"skills": [{"name": "pdfgen", "when_to_use": "generate invoice pdfs"}]}})
    # b's skill matches the prompt -> small bonus breaks the tie toward b.
    decision = runtime.router.resolve({"tags": ["x"], "prompt": "please generate an invoice", "allowed_worker_ids": [a["id"], b["id"]]})
    assert decision.worker["id"] == b["id"] and "skill hint" in decision.reason, decision.reason
    # ...but the bonus (2) is below a single tag weight (10): a's extra tag still wins.
    runtime.db.upsert_worker({"id": a["id"], "name": "aaa-skill", "base_url": base_url_mock + "/sa", "tags": ["x", "y"]})
    _online(runtime, a["id"], {})
    decision = runtime.router.resolve({"tags": ["x", "y"], "prompt": "please generate an invoice", "allowed_worker_ids": [a["id"], b["id"]]})
    assert decision.worker["id"] == a["id"], "skill hint must not overturn a tag decision"
    runtime.db.delete_worker(a["id"])
    runtime.db.delete_worker(b["id"])


def check_enable_probe(runtime: AtlasRuntime, base_url: str, base_url_mock: str, tokens: dict) -> None:
    worker = runtime.db.upsert_worker({"name": "enable-probe", "base_url": base_url_mock})

    # operator lacks the admin permission the sync-mode route requires.
    status, _, _ = request_json(base_url, "POST", f"/api/workers/{worker['id']}/sync-mode", {"sync_mode": "tunnel"}, tokens["operator"])
    assert status == 403, status

    # enabling with an UNREACHABLE/failing stat is rejected; the mode stays disabled.
    MockWorker.stat_status = 500
    status, body, _ = request_json(base_url, "POST", f"/api/workers/{worker['id']}/sync-mode", {"sync_mode": "tunnel"}, tokens["admin"])
    assert status == 400, (status, body)
    assert runtime.db.get_worker(worker["id"])["sync_mode"] == "disabled"

    # a VALID probe enables it (Mutation: skip the enable-time probe -> the 500 case above enables -> red).
    MockWorker.stat_status = 200
    MockWorker.stat_busy = False
    status, body, _ = request_json(base_url, "POST", f"/api/workers/{worker['id']}/sync-mode", {"sync_mode": "tunnel"}, tokens["admin"])
    assert status == 200, (status, body)
    assert body["worker"]["sync_mode"] == "tunnel"
    assert "token" not in body["worker"], "must never leak the worker token"

    # disabling never probes (works even when the worker is unreachable).
    MockWorker.stat_status = 500
    status, body, _ = request_json(base_url, "POST", f"/api/workers/{worker['id']}/sync-mode", {"sync_mode": "disabled"}, tokens["admin"])
    assert status == 200 and body["worker"]["sync_mode"] == "disabled", (status, body)
    MockWorker.stat_status = 200

    # the change is audited with old->new + actor.
    audits = [row for row in runtime.db.list_audit() if row["action"] == "worker.sync_mode_changed"]
    assert audits, "sync_mode change must be audited"
    latest = audits[0]  # list_audit is newest-first; the last change above disabled the mode.
    assert latest["details"]["old"] == "tunnel" and latest["details"]["new"] == "disabled", latest["details"]
    assert latest["actor"] == "admin", latest.get("actor")  # audited under the authenticated admin, not "local".


def check_dashboard(_runtime: AtlasRuntime) -> None:
    app_js = (ROOT / "atlas" / "static" / "app.js").read_text()
    # New T4 markers present (gate markers for the new dashboard elements).
    for marker in (
        "function safeHttpUrl(",
        'class="wc-busy"',
        'class="sync-mode-select"',
        'class="wc-skills"',
        "data-version-warn",
        "CONTRACT_TESTED_VERSIONS",
        "function versionOutsideContract(",
    ):
        assert marker in app_js, f"missing T4 dashboard marker: {marker}"
    # The "Open worker UI" href MUST flow through safeHttpUrl, never a raw worker-reported value.
    assert "const uiUrl = safeHttpUrl(" in app_js and 'href="${escapeHtml(uiUrl)}"' in app_js
    # Existing markers intact (no regression to the worker card / escaping).
    for existing in ('class="worker-card"', "poll-worker", "function escapeHtml("):
        assert existing in app_js, f"existing dashboard marker regressed: {existing}"
    # Actually exercise the scheme guard in node: javascript:/file:/data: -> link NOT rendered.
    fn = re.search(r"function safeHttpUrl\(.*?\n}", app_js, re.S)
    assert fn, "safeHttpUrl source not found"
    script = fn.group(0) + (
        '\nconst bad=["javascript:alert(1)","file:///etc/passwd","data:text/html,x","  JavaScript:x","vbscript:x"];'
        '\nconst good=["http://w/ui","https://w/ui"];'
        '\nfor (const u of bad) if (safeHttpUrl(u)) { console.error("LEAK",u); process.exit(1); }'
        '\nfor (const u of good) if (!safeHttpUrl(u)) { console.error("BLOCKED",u); process.exit(1); }'
    )
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)  # noqa: S603,S607
    assert result.returncode == 0, f"safeHttpUrl scheme guard failed: {result.stderr}"
    # Exercise the version-mismatch predicate in node: a tested version suppresses the warning,
    # an untested one triggers it, empty suppresses. A has()->!has() inversion flips all three.
    ver_set = re.search(r"const CONTRACT_TESTED_VERSIONS = .*?;", app_js)
    ver_fn = re.search(r"function versionOutsideContract\(.*?\n}", app_js, re.S)
    assert ver_set and ver_fn, "version predicate source not found"
    ver_script = ver_set.group(0) + "\n" + ver_fn.group(0) + (
        '\nif (versionOutsideContract("0.85.0")) { console.error("tested warned"); process.exit(1); }'
        '\nif (!versionOutsideContract("0.99.0")) { console.error("untested not warned"); process.exit(1); }'
        '\nif (versionOutsideContract("")) { console.error("empty warned"); process.exit(1); }'
    )
    ver_result = subprocess.run(["node", "-e", ver_script], capture_output=True, text=True)  # noqa: S603,S607
    assert ver_result.returncode == 0, f"version predicate failed: {ver_result.stderr}"


def create_admin_operator_tokens(runtime: AtlasRuntime) -> dict:
    tokens = {}
    for role in ("admin", "operator"):
        user = runtime.db.create_user(role, f"{role}-password", role)
        _, tokens[role] = runtime.db.create_api_token(user["id"], f"{role} worker-state check")
    return tokens


def main() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        mock = ThreadingHTTPServer(("127.0.0.1", 0), MockWorker)
        threading.Thread(target=mock.serve_forever, daemon=True).start()
        base_url_mock = f"http://127.0.0.1:{mock.server_address[1]}"

        runtime = AtlasRuntime(
            Config(
                host="127.0.0.1",
                port=0,
                db_path=root / "atlas.sqlite",
                api_token=None,
                request_timeout_seconds=2,
                enable_loopback_without_token=False,
                secret_key="worker-state-secret",
                upload_dir=root / "uploads",
            )
        )
        tokens = create_admin_operator_tokens(runtime)
        server = AtlasHttpServer(("127.0.0.1", 0), runtime)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"

        try:
            check_migration_and_default(runtime, base_url_mock)
            check_sync_mode_survives_poll(runtime, base_url_mock)
            check_poll_busy_probe_is_gated(runtime, base_url_mock)
            check_router_busy_tiebreak(runtime, base_url_mock)
            check_fixture_stability(runtime, base_url_mock)
            check_skill_hint_is_advisory(runtime, base_url_mock)
            check_enable_probe(runtime, base_url, base_url_mock, tokens)
            check_dashboard(runtime)
        finally:
            server.shutdown()
            mock.shutdown()

    print("check_worker_state OK")


if __name__ == "__main__":
    main()
