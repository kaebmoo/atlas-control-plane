"""Hermetic regression checks for the Booth and Permit PoC local proxies."""

from __future__ import annotations

import http.client
import importlib.util
import json
import sys
import threading
from pathlib import Path
from types import ModuleType

from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise AssertionError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def start_server(module: ModuleType) -> tuple[ThreadingHTTPServer, threading.Thread, int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    port = server.server_address[1]
    module.PORT = port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, port


def request(port: int, path: str, headers: dict[str, str], body: bytes = b"{}") -> tuple[int, dict]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    connection.request("POST", path, body=body, headers=headers)
    response = connection.getresponse()
    payload = json.loads(response.read() or b"{}")
    connection.close()
    return response.status, payload


def csrf_headers(module: ModuleType, port: int, *, token: bool = True, origin: str | None = None) -> dict[str, str]:
    host = f"127.0.0.1:{port}"
    headers = {"Host": host, "Origin": origin or f"http://{host}", "Content-Type": "application/json"}
    if token:
        headers["X-CSRF-Token"] = module.CSRF_TOKEN
    return headers


def check_proxy_csrf(module: ModuleType, paths: list[str], submit_body: bytes) -> None:
    calls: list[object] = []
    module.start_run = lambda *args: calls.append(args) or {"run_id": "run_test"}
    server, thread, port = start_server(module)
    try:
        for path in paths:
            status, _payload = request(
                port,
                path,
                csrf_headers(module, port, token=False, origin="https://attacker.invalid"),
                submit_body,
            )
            assert status == 403, f"cross-origin {path} returned {status}"
        status, _payload = request(port, paths[0], csrf_headers(module, port, token=False), submit_body)
        assert status == 403, f"missing CSRF token returned {status}"
        status, payload = request(port, paths[0], csrf_headers(module, port), submit_body)
        assert status == 200 and payload == {"run_id": "run_test"}, f"valid request rejected: {status} {payload}"
        assert calls, "valid same-origin request did not reach the proxy action"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def expect_atlas_error(module: ModuleType, expected: str, action) -> None:
    try:
        action()
    except module.AtlasError as error:
        assert expected in str(error), f"unexpected AtlasError: {error}"
    else:
        raise AssertionError(f"expected AtlasError containing {expected!r}")


def check_activation(module: ModuleType) -> None:
    calls: list[tuple[str, str]] = []
    details = iter([
        {
            "run": {"state": "waiting_for_human"},
            "approvals": [{"id": "approval_operator", "state": "pending", "node_key": "approval"}],
        },
        {
            "run": {"state": "waiting_for_human"},
            "approvals": [{"id": "approval_upload", "state": "pending", "node_key": "upload_ready"}],
        },
    ])

    def activation_atlas(method: str, path: str, **_kwargs):
        calls.append((method, path))
        if method == "GET":
            return next(details)
        if method == "POST":
            return {"ok": True}
        raise AssertionError(f"unexpected activation request: {method} {path}")

    module.atlas = activation_atlas
    assert module.activate_uploads("run_activation") == {"ok": True}
    assert calls == [
        ("GET", "/api/workflow-runs/run_activation"),
        ("GET", "/api/workflow-runs/run_activation"),
        ("POST", "/api/approvals/approval_upload/approve"),
    ], f"activation did not poll for and approve only upload_ready: {calls}"

    terminal_calls: list[tuple[str, str]] = []

    def terminal_atlas(method: str, path: str, **_kwargs):
        terminal_calls.append((method, path))
        return {"run": {"state": "failed"}, "approvals": []}

    module.atlas = terminal_atlas
    expect_atlas_error(module, "finished before uploads", lambda: module.activate_uploads("run_failed"))
    assert terminal_calls == [("GET", "/api/workflow-runs/run_failed")]

    original_timeout = module.UPLOAD_ACTIVATION_TIMEOUT_SECONDS
    module.UPLOAD_ACTIVATION_TIMEOUT_SECONDS = 0
    try:
        module.atlas = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("timeout should not poll"))
        expect_atlas_error(module, "upload gate did not become ready", lambda: module.activate_uploads("run_timeout"))
    finally:
        module.UPLOAD_ACTIVATION_TIMEOUT_SECONDS = original_timeout

    page = module.SCENARIO_PAGE
    assert page.index("await uploadFiles();") < page.index("await activateUploads();")
    error_render = page.index('$("#panel").innerHTML = \'<p style="color:#c33">ผิดพลาด: \'')
    cancel_cleanup = page.index("if (CFG.uploads && failedRunId) await cancelFailedRun(failedRunId);")
    assert error_render < cancel_cleanup, "upload error must render before awaiting cleanup"
    assert "const failedRunId = runId;" in page
    assert "async function cancelFailedRun(failedRunId)" in page
    assert "JSON.stringify({run_id: failedRunId})" in page


def check_cancel_proxy(module: ModuleType) -> None:
    atlas_calls: list[tuple[str, str]] = []
    module.atlas = lambda method, path, **_kwargs: atlas_calls.append((method, path)) or {"run": {"state": "cancelled"}}
    assert module.cancel_run("run/partial") == {"ok": True}
    assert atlas_calls == [("POST", "/api/workflow-runs/run%2Fpartial/cancel")]

    cancel_calls: list[str] = []
    module.cancel_run = lambda run_id: cancel_calls.append(run_id) or {"ok": True}
    server, thread, port = start_server(module)
    try:
        status, payload = request(port, "/api/cancel", csrf_headers(module, port), b'{"run_id":"run_partial"}')
        assert status == 200 and payload == {"ok": True}
        assert cancel_calls == ["run_partial"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def check_worker_setup() -> None:
    setup = load_module("check_booth_setup", ROOT / "poc" / "booth_demo" / "setup.py")
    requests: list[tuple[str, str, object]] = []

    def fake_api(method: str, path: str, body=None):
        requests.append((method, path, body))
        if method == "GET" and path == "/api/workers":
            return [{"id": "worker_existing", "name": "booth-reporter", "base_url": "http://old.invalid"}]
        if method == "POST" and path == "/api/workers":
            return {"id": body["id"]}
        if method == "POST" and path.endswith("/poll"):
            return {"status": "online"}
        raise AssertionError(f"unexpected setup request: {method} {path}")

    setup.api = fake_api
    assert setup.ensure_worker("booth-reporter", "http://new.invalid", "token", "reporter", "booth") == "worker_existing"
    upsert = next(body for method, path, body in requests if method == "POST" and path == "/api/workers")
    assert upsert["id"] == "worker_existing", "worker URL change did not use the stable existing ID"

    news = setup.news_graph("worker_reporter", "worker_anchor")
    permit = setup.permit_graph("worker_reporter", "worker_anchor")
    news_nodes = {node["id"]: node for node in news["nodes"]}
    permit_nodes = {node["id"]: node for node in permit["nodes"]}
    assert news_nodes["reporter"]["worker_id"] == "worker_reporter"
    assert news_nodes["anchor"]["worker_id"] == "worker_anchor"
    assert news_nodes["publish"]["worker_id"] == "worker_reporter"
    assert permit["start"] == "upload_ready"
    assert permit_nodes["upload_ready"]["type"] == "human_gate"
    assert permit_nodes["intake"]["worker_id"] == "worker_reporter"
    assert permit_nodes["examiner"]["worker_id"] == "worker_anchor"
    assert any(edge["from"] == "upload_ready" and edge["to"] == "intake" for edge in permit["edges"])


def main() -> None:
    booth = load_module("check_booth_app", ROOT / "poc" / "booth_demo" / "app.py")
    check_proxy_csrf(
        booth,
        ["/api/submit", "/api/upload?run_id=run_test&name=file.txt", "/api/activate", "/api/cancel", "/api/decide"],
        b'{"scenario":"news","fields":{}}',
    )
    check_activation(booth)
    check_cancel_proxy(booth)
    permit = load_module("check_permit_app", ROOT / "poc" / "permit_web" / "app.py")
    check_proxy_csrf(permit, ["/api/submit", "/api/decide"], b"{}")
    check_worker_setup()
    print("booth poc check ok")


if __name__ == "__main__":
    main()
