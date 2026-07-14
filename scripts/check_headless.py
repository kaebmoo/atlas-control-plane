"""Hermetic check for the headless API / web UI split (docs/plans/headless-ui-split-plan.md).

Own temp DB per server, ephemeral ports throughout (including the scripts/serve_ui.py
subprocess, which reports its bound port on stdout rather than a pre-guessed port), no
network beyond loopback. Bootstrap style mirrors scripts/check_workflow_api.py.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.app import AtlasHttpServer, AtlasRuntime
from atlas.config import Config


def _start_server(tmp: Path, name: str, **config_overrides) -> tuple[AtlasRuntime, AtlasHttpServer, threading.Thread, str]:
    runtime = AtlasRuntime(
        Config(
            host="127.0.0.1",
            port=0,
            db_path=tmp / f"{name}.sqlite",
            api_token=None,
            request_timeout_seconds=5,
            enable_loopback_without_token=True,
            upload_dir=tmp / f"{name}-uploads",
            **config_overrides,
        )
    )
    server = AtlasHttpServer(("127.0.0.1", 0), runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    return runtime, server, thread, base_url


def _stop_server(runtime: AtlasRuntime, server: AtlasHttpServer, thread: threading.Thread) -> None:
    runtime.close()  # stop the reaper daemon before the tempdir exits
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def get(base_url: str, path: str) -> tuple[int, str, dict[str, str]]:
    try:
        with urllib.request.urlopen(base_url + path, timeout=5) as response:
            return response.status, response.read().decode("utf-8"), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), dict(exc.headers)


def options(base_url: str, path: str, origin: str | None) -> dict[str, str]:
    req = urllib.request.Request(base_url + path, method="OPTIONS", headers={"Origin": origin} if origin else {})
    with urllib.request.urlopen(req, timeout=5) as response:
        return dict(response.headers)


def check_env_wiring() -> None:
    """Config.from_env() parses ATLAS_SERVE_UI / ATLAS_CORS_ORIGINS; unset must default to
    today's behavior (UI served, CORS `*`) so existing deployments are unaffected."""
    saved = {key: os.environ.get(key) for key in ("ATLAS_SERVE_UI", "ATLAS_CORS_ORIGINS")}
    try:
        os.environ.pop("ATLAS_SERVE_UI", None)
        os.environ.pop("ATLAS_CORS_ORIGINS", None)
        assert Config.from_env().serve_ui is True, "serve_ui must default to True when unset"
        assert Config.from_env().cors_origins == (), "cors_origins must default to () when unset"

        os.environ["ATLAS_SERVE_UI"] = "0"
        assert Config.from_env().serve_ui is False
        os.environ["ATLAS_SERVE_UI"] = "true"
        assert Config.from_env().serve_ui is True

        os.environ["ATLAS_CORS_ORIGINS"] = "http://a.example, http://b.example"
        assert Config.from_env().cors_origins == ("http://a.example", "http://b.example")
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def check_headless_mode(tmp: Path) -> None:
    runtime, server, thread, base_url = _start_server(tmp, "headless", serve_ui=False)
    try:
        status, body, _ = get(base_url, "/")
        assert status == HTTPStatus.NOT_FOUND, f"headless GET / expected 404, got {status}"
        assert json.loads(body) == {"error": "not found"}

        status, body, _ = get(base_url, "/static/app.js")
        assert status == HTTPStatus.NOT_FOUND, f"headless GET /static/app.js expected 404, got {status}"
        assert json.loads(body) == {"error": "not found"}

        status, body, _ = get(base_url, "/healthz")
        assert status == HTTPStatus.OK, "headless /healthz must stay 200 (above the static fallthrough)"
        assert json.loads(body)["ok"] is True

        status, body, _ = get(base_url, "/api/workers")
        assert status == HTTPStatus.OK, f"headless authed /api/workers expected 200, got {status}: {body}"
    finally:
        _stop_server(runtime, server, thread)


def check_default_mode(tmp: Path) -> None:
    runtime, server, thread, base_url = _start_server(tmp, "default", serve_ui=True)
    try:
        status, body, _ = get(base_url, "/")
        assert status == HTTPStatus.OK, f"default GET / expected 200, got {status}"
        assert 'id="loginScreen"' in body, "index.html marker missing"

        status, body, _ = get(base_url, "/static/config.js")
        assert status == HTTPStatus.OK
        assert 'window.ATLAS_API_BASE = "";' in body, "shipped config.js default must be the empty (same-origin) base"
    finally:
        _stop_server(runtime, server, thread)


def check_cors_allowlist(tmp: Path) -> None:
    runtime, server, thread, base_url = _start_server(tmp, "cors", cors_origins=("http://ui.example",))
    try:
        headers = options(base_url, "/api/workers", origin="http://ui.example")
        assert headers.get("Access-Control-Allow-Origin") == "http://ui.example", headers
        assert headers.get("Vary") == "Origin", headers
        assert "Access-Control-Allow-Credentials" not in headers

        # Not just a disjoint origin — a superstring/prefix-confusable one, so a regression from
        # exact membership to startswith()/substring matching would also be caught.
        headers = options(base_url, "/api/workers", origin="http://ui.example.evil.com")
        assert "Access-Control-Allow-Origin" not in headers, headers

        headers = options(base_url, "/api/workers", origin="http://evil.example")
        assert "Access-Control-Allow-Origin" not in headers, headers

        # No Origin header (same-origin/non-browser caller) must be unaffected by the allowlist too.
        headers = options(base_url, "/api/workers", origin=None)
        assert "Access-Control-Allow-Origin" not in headers, headers
    finally:
        _stop_server(runtime, server, thread)

    # No allowlist configured (default) => today's behavior, unconditionally '*'.
    runtime, server, thread, base_url = _start_server(tmp, "cors-default")
    try:
        headers = options(base_url, "/api/workers", origin="http://anything.example")
        assert headers.get("Access-Control-Allow-Origin") == "*", headers
        headers = options(base_url, "/api/workers", origin=None)
        assert headers.get("Access-Control-Allow-Origin") == "*", headers
    finally:
        _stop_server(runtime, server, thread)


def check_api_base_wiring(tmp: Path) -> None:
    """Static assertions (gate-marker style): the dashboard prefixes every API call with
    API_BASE, and index.html loads config.js before app.js."""
    runtime, server, thread, base_url = _start_server(tmp, "wiring", serve_ui=True)
    try:
        _, html, _ = get(base_url, "/")
        _, javascript, _ = get(base_url, "/static/app.js")

        assert html.index('src="/static/config.js') < html.index('src="/static/app.js'), (
            "index.html must load config.js before app.js"
        )
        assert "const API_BASE" in javascript
        assert "fetch(API_BASE + path" in javascript, "central api() helper must prefix API_BASE"
        assert "${API_BASE}/api/usage?" in javascript, "usage CSV export fetch must prefix API_BASE"
        assert "${API_BASE}/api/artifacts/" in javascript, "artifact content fetch must prefix API_BASE"
        assert "${API_BASE}/api/jobs/${jobId}/events" in javascript, "SSE-over-fetch must prefix API_BASE"
    finally:
        _stop_server(runtime, server, thread)


def check_node_syntax() -> None:
    result = subprocess.run(["node", "--check", str(ROOT / "atlas" / "static" / "app.js")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


_PORT_LINE = re.compile(r"^PORT=(\d+)$")


def check_dev_ui_server() -> None:
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "serve_ui.py"), "--port", "0", "--api-base", "http://127.0.0.1:9"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        first_line = proc.stdout.readline().strip()
        match = _PORT_LINE.match(first_line)
        assert match, f"serve_ui.py did not report its bound port; got {first_line!r}"
        port = int(match.group(1))
        base_url = f"http://127.0.0.1:{port}"

        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                status, body, _ = get(base_url, "/static/config.js")
                break
            except (urllib.error.URLError, ConnectionError, socket.timeout) as exc:
                last_error = exc
                time.sleep(0.05)
        else:
            raise AssertionError(f"serve_ui.py never became reachable on {base_url}: {last_error}")

        assert status == HTTPStatus.OK
        assert "http://127.0.0.1:9" in body, body
        assert "no-store" in get(base_url, "/static/config.js")[2].get("Cache-Control", "")

        status, body, _ = get(base_url, "/")
        assert status == HTTPStatus.OK and "<title>Atlas Control Plane</title>" in body

        status, body, _ = get(base_url, "/some/spa/route")
        assert status == HTTPStatus.OK and "<title>Atlas Control Plane</title>" in body

        # A real on-disk static asset must resolve through the /static/* branch, not fall
        # through to the SPA-fallback index.html (the two branches share a prefix check).
        status, body, headers = get(base_url, "/static/app.js")
        assert status == HTTPStatus.OK and "const API_BASE" in body, "must serve real app.js, not index.html"
        assert headers.get("Content-Type") == "application/javascript", headers

        # A missing /static/* file must 404 like AtlasHandler._handle_static, NOT SPA-fallback
        # to index.html — that would silently mask a broken/renamed asset reference in dev.
        status, body, _ = get(base_url, "/static/does-not-exist.js")
        assert status == HTTPStatus.NOT_FOUND, f"missing static asset must 404, got {status}: {body!r}"
        assert json.loads(body) == {"error": "not found"}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def main() -> None:
    check_env_wiring()
    with TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        check_headless_mode(tmp_path)
        check_default_mode(tmp_path)
        check_cors_allowlist(tmp_path)
        check_api_base_wiring(tmp_path)
    check_node_syntax()
    check_dev_ui_server()
    print("headless check ok")


if __name__ == "__main__":
    main()
