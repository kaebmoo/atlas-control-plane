from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import warnings
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.app import AtlasHttpServer, AtlasRuntime
from atlas.admin import main as admin_main
from atlas.auth import LoginRateLimiter, hash_api_token
from atlas.config import Config
from atlas.db import Database


WORKER_TOKEN = "worker-secret-value"


class MockThClawsHandler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        assert self.headers.get("Authorization") == f"Bearer {WORKER_TOKEN}"
        payload = {"ok": True} if self.path == "/healthz" else {"name": "mock-thclaws"}
        self._json(payload)

    def do_POST(self) -> None:
        assert self.headers.get("Authorization") == f"Bearer {WORKER_TOKEN}"
        assert self.path == "/agent/run"
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        body = b"event: text\ndata: ok\n\nevent: done\ndata: [DONE]\n\n"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    with TemporaryDirectory() as tmp:
        check_secure_config_default()
        check_login_limiter_keying_and_restart()
        check_worker_token_migration(Path(tmp) / "legacy.sqlite")
        mock = ThreadingHTTPServer(("127.0.0.1", 0), MockThClawsHandler)
        mock_thread = threading.Thread(target=mock.serve_forever, daemon=True)
        mock_thread.start()

        db_path = Path(tmp) / "atlas.sqlite"
        admin_token, operator_token = seed_users_with_cli(db_path)
        runtime = AtlasRuntime(
            Config(
                host="127.0.0.1",
                port=0,
                db_path=db_path,
                api_token="legacy-bootstrap-secret",
                request_timeout_seconds=2,
                enable_loopback_without_token=False,
                secret_key="test-atlas-secret-key",
                upload_dir=Path(tmp) / "uploads",
                max_active_sessions=5,
                login_rate_limit_attempts=2,
                login_rate_limit_window_seconds=60,
                login_rate_limit_cooldown_seconds=1,
            )
        )
        with sqlite3.connect(db_path) as conn:
            admin_password_hash = conn.execute("SELECT password_hash FROM users WHERE username = 'admin'").fetchone()[0]
            stored_api_hashes = [row[0] for row in conn.execute("SELECT token_hash FROM api_tokens")]
        assert admin_password_hash != "admin-password" and "admin-password" not in admin_password_hash
        assert all(admin_token != token_hash and operator_token != token_hash for token_hash in stored_api_hashes)

        worker = runtime.db.upsert_worker(
            {
                "name": "Encrypted mock",
                "base_url": f"http://127.0.0.1:{mock.server_address[1]}",
                "token": WORKER_TOKEN,
            }
        )
        with sqlite3.connect(db_path) as conn:
            stored_token = conn.execute("SELECT token FROM workers WHERE id = ?", (worker["id"],)).fetchone()[0]
        assert stored_token != WORKER_TOKEN and stored_token.startswith("atlasenc:v1:")
        assert runtime.db.get_worker(worker["id"])["token"] == WORKER_TOKEN

        server = AtlasHttpServer(("127.0.0.1", 0), runtime)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            assert request(base_url, "GET", "/api/me")[0] == 401
            assert request(base_url, "GET", "/api/me", token="invalid-token")[0] == 401

            status, created = request(
                base_url,
                "POST",
                "/api/users",
                {"username": "viewer", "password": "viewer-password", "role": "viewer"},
                admin_token,
            )
            assert status == 201 and created["user"]["role"] == "viewer"
            viewer = created["user"]
            status, issued = request(
                base_url,
                "POST",
                "/api/tokens",
                {"user_id": viewer["id"], "name": "viewer check"},
                admin_token,
            )
            assert status == 201
            viewer_token = issued["api_token"]
            renamed = request(
                base_url,
                "PUT",
                f"/api/tokens/{issued['token']['id']}",
                {"name": "viewer renamed"},
                admin_token,
            )
            assert renamed[0] == 200 and renamed[1]["token"]["name"] == "viewer renamed"
            assert request(base_url, "GET", "/api/me", token=viewer_token)[1]["user"]["username"] == "viewer"

            # The per-(normalized username, peer IP) limiter rejects before expensive
            # password verification. It returns a deterministic 429 + Retry-After,
            # while a different username is not collateral damage.
            assert request(base_url, "POST", "/api/auth/login", {"username": "viewer", "password": "wrong"})[0] == 401
            status, limited, headers = request_with_headers(
                base_url, "POST", "/api/auth/login", {"username": "viewer", "password": "wrong"}
            )
            assert status == 429 and limited["error"] == "too many login attempts; retry later"
            assert int(headers["Retry-After"]) >= 1
            assert request(base_url, "POST", "/api/auth/login", {"username": "different", "password": "wrong"})[0] == 401
            time.sleep(1.05)

            status, _ = request(
                base_url,
                "POST",
                "/api/jobs",
                {"worker_id": worker["id"], "prompt": "viewer must not run"},
                viewer_token,
            )
            assert status == 403
            status, job_payload = request(
                base_url,
                "POST",
                "/api/jobs",
                {"worker_id": worker["id"], "prompt": "operator may run"},
                operator_token,
            )
            assert status == 202 and job_payload["job"]["id"]
            wait_for_job(runtime, job_payload["job"]["id"])
            assert any(row["action"] == "job.create" and row["actor"] == "operator" for row in runtime.db.list_audit(100))

            status, polled = request(base_url, "POST", f"/api/workers/{worker['id']}/poll", {}, operator_token)
            assert status == 200 and polled["worker"]["status"] == "online"
            assert runtime.db.get_worker(worker["id"])["token"] == WORKER_TOKEN

            _, disposable = request(
                base_url,
                "POST",
                "/api/tokens",
                {"user_id": viewer["id"], "name": "revoke me"},
                admin_token,
            )
            disposable_token = disposable["api_token"]
            disposable_id = disposable["token"]["id"]
            assert request(base_url, "DELETE", f"/api/tokens/{disposable_id}", token=admin_token)[0] == 200
            assert request(base_url, "GET", "/api/me", token=disposable_token)[0] == 401

            status, logged_in = request(
                base_url,
                "POST",
                "/api/auth/login",
                {"username": "viewer", "password": "viewer-password"},
            )
            assert status == 200 and logged_in["user"]["username"] == "viewer"
            login_token = logged_in["token"]
            assert logged_in["session"]["expires_at"] and request(base_url, "GET", "/api/me", token=login_token)[1]["session"] == logged_in["session"]
            assert request(base_url, "POST", "/api/auth/logout", {}, login_token)[0] == 200
            assert request(base_url, "GET", "/api/me", token=login_token)[0] == 401

            # A new login is a session (not a named API token). At the configured cap,
            # only the oldest session is revoked; the independently-issued API token
            # remains usable.
            sessions: list[str] = []
            for _ in range(runtime.config.max_active_sessions + 1):
                status, session = request(
                    base_url, "POST", "/api/auth/login", {"username": "viewer", "password": "viewer-password"}
                )
                assert status == 200 and session["session"]["expires_at"]
                sessions.append(session["token"])
            assert request(base_url, "GET", "/api/me", token=sessions[0])[0] == 401
            assert request(base_url, "GET", "/api/me", token=viewer_token)[0] == 200

            # Expiry is enforced for every bearer path, including EventSource query
            # auth. The raw token/password never enter the audit log.
            with runtime.db.connect() as conn:
                conn.execute(
                    "UPDATE api_tokens SET expires_at = ? WHERE token_hash = ?",
                    ("2000-01-01T00:00:00Z", hash_api_token(sessions[-1])),
                )
            assert request(base_url, "GET", "/api/me", token=sessions[-1])[0] == 401
            assert _raw_status(f"{base_url}/api/jobs/not-a-real-job/events?token={sessions[-1]}") == 401

            status, legacy_me = request(base_url, "GET", "/api/me", token="legacy-bootstrap-secret")
            assert status == 200 and legacy_me["user"]["role"] == "admin"
            assert any(row["action"] == "user.create" and row["actor"] == "admin" for row in runtime.db.list_audit(100))
            audit_text = json.dumps(runtime.db.list_audit(200), sort_keys=True)
            assert "auth.session_cap_revoked" in audit_text and "auth.session_expired" in audit_text and "auth.login_rate_limited" in audit_text
            assert "viewer-password" not in audit_text and sessions[-1] not in audit_text and login_token not in audit_text

            # ?token= query auth is restricted to the SSE event streams (EventSource can't set a
            # header); it must be REJECTED on any normal endpoint so tokens don't leak into URLs.
            conv = runtime.db.create_conversation({"title": "t"})
            term_job = runtime.db.create_job({"conversation_id": conv["id"], "worker_id": worker["id"], "prompt": "x"})
            runtime.db.update_job(term_job["id"], state="succeeded")
            assert _raw_status(f"{base_url}/api/me?token={admin_token}") == 401, "query token must be rejected on /api/me"
            assert _raw_status(f"{base_url}/api/jobs/{term_job['id']}/events?after=0&token={admin_token}") == 200, "query token must work for SSE events"
        finally:
            runtime.close()  # stop the reaper daemon before the tempdir exits
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)
            mock.shutdown()
            mock.server_close()
            mock_thread.join(timeout=2)

    print("auth check ok")


def check_secure_config_default() -> None:
    previous = os.environ.pop("ATLAS_LOOPBACK_NO_AUTH", None)
    try:
        assert Config.from_env().enable_loopback_without_token is False
    finally:
        if previous is not None:
            os.environ["ATLAS_LOOPBACK_NO_AUTH"] = previous


def check_login_limiter_keying_and_restart() -> None:
    limiter = LoginRateLimiter(2, 60, 60)
    assert limiter.record_failure("Alice", "192.0.2.1").allowed
    assert not limiter.record_failure("alice", "192.0.2.1").allowed, "username normalization must share a bucket"
    assert limiter.check("alice", "192.0.2.2").allowed, "a different peer IP must not share a bucket"
    assert limiter.check("bob", "192.0.2.1").allowed, "a different username must not share a bucket"
    assert LoginRateLimiter(2, 60, 60).check("alice", "192.0.2.1").allowed, "restart resets in-memory limiter state"


def check_worker_token_migration(path: Path) -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        plaintext_db = Database(path, secret_key="")
        worker = plaintext_db.upsert_worker(
            {"name": "Legacy plaintext", "base_url": "http://127.0.0.1:1", "token": "legacy-worker-token"}
        )
    assert any("ATLAS_SECRET_KEY is unset" in str(item.message) for item in caught)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT token FROM workers WHERE id = ?", (worker["id"],)).fetchone()[0] == "legacy-worker-token"
    encrypted_db = Database(path, secret_key="migration-key")
    assert encrypted_db.get_worker(worker["id"])["token"] == "legacy-worker-token"
    encrypted_db.update_worker_status(worker["id"], "online")
    with sqlite3.connect(path) as conn:
        stored = conn.execute("SELECT token FROM workers WHERE id = ?", (worker["id"],)).fetchone()[0]
    assert stored.startswith("atlasenc:v1:") and "legacy-worker-token" not in stored
    assert encrypted_db.get_worker(worker["id"])["token"] == "legacy-worker-token"


def seed_users_with_cli(db_path: Path) -> tuple[str, str]:
    previous_db = os.environ.get("ATLAS_DB")
    previous_key = os.environ.get("ATLAS_SECRET_KEY")
    os.environ["ATLAS_DB"] = str(db_path)
    os.environ["ATLAS_SECRET_KEY"] = "test-atlas-secret-key"

    def run(*args: str, password: str | None = None) -> str:
        output = io.StringIO()
        password_prompt = mock.patch("atlas.admin.getpass.getpass", return_value=password) if password is not None else contextlib.nullcontext()
        with contextlib.redirect_stdout(output), password_prompt:
            admin_main(list(args))
        return output.getvalue()

    try:
        admin_output = run("create-admin", "admin", password="admin-password")
        admin_token = value_after(admin_output, "One-time token: ")
        run("create-user", "operator", "--role", "operator", password="operator-password")
        operator_output = run("create-token", "operator", "--name", "operator check")
        operator_token = value_after(operator_output, "One-time token: ")
        disposable_output = run("create-token", "operator", "--name", "cli revoke check")
        disposable_id = value_after(disposable_output, "Token id: ")
        assert f"Revoked {disposable_id}" in run("revoke-token", disposable_id)
        listed = json.loads(run("list-users"))
        assert {user["username"] for user in listed["users"]} == {"admin", "operator"}
        return admin_token, operator_token
    finally:
        if previous_db is None:
            os.environ.pop("ATLAS_DB", None)
        else:
            os.environ["ATLAS_DB"] = previous_db
        if previous_key is None:
            os.environ.pop("ATLAS_SECRET_KEY", None)
        else:
            os.environ["ATLAS_SECRET_KEY"] = previous_key


def value_after(output: str, prefix: str) -> str:
    return next(line.removeprefix(prefix) for line in output.splitlines() if line.startswith(prefix))


def _raw_status(url: str) -> int:
    """GET a URL (token in the query string) and return the HTTP status, tolerating an SSE body."""
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            response.read(1)
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def request(
    base_url: str,
    method: str,
    path: str,
    payload: dict | None = None,
    token: str | None = None,
) -> tuple[int, dict]:
    status, response, _headers = request_with_headers(base_url, method, path, payload, token)
    return status, response


def request_with_headers(
    base_url: str,
    method: str,
    path: str,
    payload: dict | None = None,
    token: str | None = None,
) -> tuple[int, dict, dict[str, str]]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base_url + path, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read() or b"{}"), dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}"), dict(exc.headers.items())


def wait_for_job(runtime: AtlasRuntime, job_id: str) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = runtime.db.get_job(job_id)
        if job and job["state"] in {"succeeded", "failed", "cancelled"}:
            assert job["state"] == "succeeded", job
            return
        time.sleep(0.02)
    raise AssertionError(f"job did not finish: {runtime.db.get_job(job_id)}")


if __name__ == "__main__":
    main()
