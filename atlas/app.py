from __future__ import annotations

import argparse
import json
import mimetypes
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import Config
from .db import Database
from .jobs import JobManager, TERMINAL_STATES
from .router import Router


STATIC_DIR = Path(__file__).parent / "static"


class AtlasRuntime:
    def __init__(self, config: Config):
        self.config = config
        self.db = Database(config.db_path)
        self.jobs = JobManager(self.db, config.request_timeout_seconds)
        self.router = Router(self.db)


class AtlasHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], runtime: AtlasRuntime):
        super().__init__(server_address, AtlasHandler)
        self.runtime = runtime


class AtlasHandler(BaseHTTPRequestHandler):
    server: AtlasHttpServer
    protocol_version = "HTTP/1.1"

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path.startswith("/api/"):
                if not self._is_authorized():
                    self._json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
                    return
                self._handle_api(method, path, parse_qs(parsed.query))
                return
            self._handle_static(path)
        except ValueError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except FileNotFoundError:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except BrokenPipeError:
            return
        except Exception as exc:
            self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_api(self, method: str, path: str, query: dict[str, list[str]]) -> None:
        runtime = self.server.runtime
        parts = [part for part in path.split("/") if part]

        if method == "GET" and parts == ["api", "health"]:
            self._json(
                {
                    "ok": True,
                    "service": "atlas-control-plane",
                    "db": str(runtime.config.db_path),
                    "workers": len(runtime.db.list_workers()),
                }
            )
            return

        if parts == ["api", "workers"]:
            if method == "GET":
                self._json({"workers": [_public_worker(worker) for worker in runtime.db.list_workers()]})
                return
            if method == "POST":
                payload = self._read_json()
                worker = runtime.db.upsert_worker(payload)
                self._json({"worker": _public_worker(worker)}, HTTPStatus.CREATED)
                return

        if parts == ["api", "workers", "poll"] and method == "POST":
            self._json({"workers": [_public_worker(worker) for worker in runtime.jobs.poll_all_workers()]})
            return

        if len(parts) == 3 and parts[:2] == ["api", "workers"]:
            worker_id = parts[2]
            if method == "GET":
                worker = runtime.db.get_worker(worker_id)
                if not worker:
                    raise FileNotFoundError()
                self._json({"worker": _public_worker(worker)})
                return
            if method == "DELETE":
                if not runtime.db.delete_worker(worker_id):
                    raise FileNotFoundError()
                self._json({"deleted": True})
                return

        if len(parts) == 4 and parts[:2] == ["api", "workers"] and parts[3] == "poll" and method == "POST":
            self._json({"worker": _public_worker(runtime.jobs.poll_worker(parts[2]))})
            return

        if parts == ["api", "workspaces"]:
            if method == "GET":
                self._json({"workspaces": runtime.db.list_workspaces()})
                return
            if method == "POST":
                workspace = runtime.db.upsert_workspace(self._read_json())
                self._json({"workspace": workspace}, HTTPStatus.CREATED)
                return

        if len(parts) == 3 and parts[:2] == ["api", "workspaces"]:
            workspace_id = parts[2]
            if method == "GET":
                workspace = runtime.db.get_workspace(workspace_id)
                if not workspace:
                    raise FileNotFoundError()
                self._json({"workspace": workspace})
                return
            if method == "DELETE":
                if not runtime.db.delete_workspace(workspace_id):
                    raise FileNotFoundError()
                self._json({"deleted": True})
                return

        if parts == ["api", "conversations"]:
            if method == "GET":
                self._json({"conversations": runtime.db.list_conversations()})
                return
            if method == "POST":
                conversation = runtime.db.create_conversation(self._read_json())
                self._json({"conversation": conversation}, HTTPStatus.CREATED)
                return

        if parts == ["api", "routes", "resolve"] and method == "POST":
            decision = runtime.router.resolve(self._read_json())
            self._json(
                {
                    "worker": _public_worker(decision.worker),
                    "workspace": decision.workspace,
                    "reason": decision.reason,
                    "thclaws_session_id": decision.thclaws_session_id,
                }
            )
            return

        if parts == ["api", "jobs"]:
            if method == "GET":
                limit = int(query.get("limit", ["100"])[0])
                self._json({"jobs": runtime.db.list_jobs(limit)})
                return
            if method == "POST":
                job = runtime.jobs.submit(self._read_json())
                self._json({"job": job}, HTTPStatus.ACCEPTED)
                return

        if len(parts) == 3 and parts[:2] == ["api", "jobs"]:
            job = runtime.db.get_job(parts[2])
            if not job:
                raise FileNotFoundError()
            if method == "GET":
                self._json({"job": job})
                return

        if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "cancel" and method == "POST":
            self._json({"job": runtime.jobs.cancel(parts[2])})
            return

        if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "events" and method == "GET":
            after = int(query.get("after", ["0"])[0])
            self._stream_job_events(parts[2], after)
            return

        if parts == ["api", "audit"] and method == "GET":
            limit = int(query.get("limit", ["100"])[0])
            self._json({"audit": runtime.db.list_audit(limit)})
            return

        raise FileNotFoundError()

    def _handle_static(self, path: str) -> None:
        if path in {"", "/"}:
            target = STATIC_DIR / "index.html"
        elif path.startswith("/static/"):
            target = STATIC_DIR / path.removeprefix("/static/")
        elif path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        else:
            target = STATIC_DIR / "index.html"
        target = target.resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.exists():
            raise FileNotFoundError()
        content = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self._cors_headers()
        self.send_header("Content-Type", mimetypes.guess_type(str(target))[0] or "application/octet-stream")
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _stream_job_events(self, job_id: str, after: int) -> None:
        runtime = self.server.runtime
        if not runtime.db.get_job(job_id):
            raise FileNotFoundError()
        self.send_response(HTTPStatus.OK)
        self._cors_headers()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        last_seq = after
        while True:
            rows = runtime.db.get_job_events_after(job_id, last_seq, limit=100)
            for row in rows:
                last_seq = int(row["seq"])
                payload = dict(row.get("payload") or {})
                payload.setdefault("seq", last_seq)
                payload.setdefault("created_at", row.get("created_at"))
                self._write_sse(row["event_type"], payload, last_seq)

            job = runtime.db.get_job(job_id)
            if not job or (job["state"] in TERMINAL_STATES and not rows):
                self._write_sse("close", {"state": job["state"] if job else "missing"}, last_seq + 1)
                self.close_connection = True
                break
            time.sleep(0.4)

    def _write_sse(self, event: str, payload: Any, event_id: int) -> None:
        data = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        self.wfile.write(f"id: {event_id}\n".encode("utf-8"))
        self.wfile.write(f"event: {event}\n".encode("utf-8"))
        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "authorization, content-type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")

    def _is_authorized(self) -> bool:
        token = self.server.runtime.config.api_token
        if not token:
            return True
        if self.server.runtime.config.enable_loopback_without_token and self.client_address[0] in {"127.0.0.1", "::1"}:
            return True
        query_token = parse_qs(urlparse(self.path).query).get("token", [None])[0]
        if query_token == token:
            return True
        return self.headers.get("Authorization") == f"Bearer {token}"


def run_server(config: Config) -> None:
    runtime = AtlasRuntime(config)
    server = AtlasHttpServer((config.host, config.port), runtime)
    print(f"Atlas listening on {config.base_url}")
    print(f"SQLite state: {config.db_path}")
    server.serve_forever()


def _public_worker(worker: dict[str, Any]) -> dict[str, Any]:
    public = dict(worker)
    token = public.pop("token", None)
    public["token_set"] = bool(token)
    return public


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Atlas control plane")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--db", default=None)
    args = parser.parse_args(argv)

    config = Config.from_env()
    if args.host or args.port or args.db:
        config = Config(
            host=args.host or config.host,
            port=args.port or config.port,
            db_path=Path(args.db).resolve() if args.db else config.db_path,
            api_token=config.api_token,
            request_timeout_seconds=config.request_timeout_seconds,
            enable_loopback_without_token=config.enable_loopback_without_token,
        )
    run_server(config)
