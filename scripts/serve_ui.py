"""Dev-only static server for split-mode UI development.

Serves `atlas/static/` on its own port while the Atlas API runs headless
(`ATLAS_SERVE_UI=0`) on another. Mirrors `AtlasHandler._handle_static`'s
path resolution exactly: a `/static/*` miss (or traversal) 404s, everything
else SPA-falls-back to `index.html`. Injects `window.ATLAS_API_BASE` for
`/static/config.js` instead of serving the on-disk file, so the shipped
default is never modified. Stdlib only; never used in production (see
scripts/run-prod.sh / atlas/app.py for that).
"""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATHS = {"/static/config.js", "/config.js"}


def make_handler(static_dir: Path, api_base: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path in CONFIG_PATHS:
                # json.dumps produces a safe JS string literal — api_base is a CLI arg and must
                # never be allowed to break out of the assignment into arbitrary script.
                self._send(f"window.ATLAS_API_BASE = {json.dumps(api_base)};\n".encode(), "application/javascript")
                return
            if path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path.startswith("/static/"):
                # Mirrors AtlasHandler._handle_static: a /static/* miss (or a traversal attempt
                # that escapes static_dir) is a 404, NOT an SPA-fallback — only unmatched
                # non-static routes fall back to index.html.
                target = (static_dir / path.removeprefix("/static/")).resolve()
                if not str(target).startswith(str(static_dir.resolve())) or not target.exists():
                    self._send_404()
                    return
            else:
                target = static_dir / "index.html"
            content_type = {
                ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
                ".png": "image/png", ".svg": "image/svg+xml", ".json": "application/json",
            }.get(target.suffix, "application/octet-stream")
            self._send(target.read_bytes(), content_type)

        def _send(self, body: bytes, content_type: str) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.end_headers()
            self.wfile.write(body)

        def _send_404(self) -> None:
            body = json.dumps({"error": "not found"}).encode()
            self.send_response(HTTPStatus.NOT_FOUND)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Dev static server for the Atlas dashboard (split mode)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--api-base", required=True, help="e.g. http://127.0.0.1:8787")
    parser.add_argument("--static-dir", default=str(ROOT / "atlas" / "static"))
    args = parser.parse_args(argv)

    static_dir = Path(args.static_dir).resolve()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(static_dir, args.api_base))
    port = server.server_address[1]
    # First line is machine-readable (ephemeral-port checks parse it) before the human message.
    print(f"PORT={port}", flush=True)
    print(f"Serving {static_dir} on http://127.0.0.1:{port} (API_BASE={args.api_base})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
