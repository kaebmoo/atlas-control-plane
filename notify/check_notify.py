"""Hermetic check for the Atlas Notify sidecar (notify/notify.py).

Everything runs in-process on loopback: a temp SQLite store, a ~30-line fake SMTP
server (smtpd left the stdlib in 3.12), a Telegram HTTP stub with an optional
response gate, and real HTTP POSTs against a live NotifyServer.

Mutation targets (break the code; this check must go red; revert byte-identical):
  A. drop signature verification in Handler.do_POST ............ scenario 1
  B. verify over json.dumps(json.loads(raw)) instead of raw .... scenario 2
  C. notify without Store.claim (unconditional fan-out) ........ scenario 4
  D. make dedup in-memory (dict) instead of SQLite ............. scenario 4 restart leg
  E. drop the walk-down in targets_for (jump to fallback) ...... scenario 5
  F. remove the per-target try/except in Notifier.handle ....... scenario 6
  G. move the 204 response after the fan-out ................... scenario 8
  H. drop _one_line escaping from the notified log line ........ scenario 9
  I. drop _one_line from send_smtp's Subject header ............ scenario 9 header-injection leg
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import socket
import socketserver
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from notify.notify import LOGGER, Notifier, NotifyServer, Store, load_config  # noqa: E402

SECRET = "check-secret-key"
TELEGRAM_TOKEN = "tok_check_telegram_secret"  # must never appear in logs


# --- stubs -------------------------------------------------------------------


class FakeSMTP(socketserver.ThreadingTCPServer):
    """Just enough SMTP to receive one message: 220 / EHLO / MAIL / RCPT / DATA / QUIT."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self) -> None:
        self.messages: list[tuple[list[str], str]] = []  # (rcpts, data)
        super().__init__(("127.0.0.1", 0), _SMTPSession)


class _SMTPSession(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        assert isinstance(self.server, FakeSMTP)
        self.wfile.write(b"220 fake-smtp\r\n")
        rcpts: list[str] = []
        while True:
            line = self.rfile.readline()
            if not line:
                return
            command = line.decode("utf-8", "replace").strip()
            upper = command.upper()
            if upper.startswith("QUIT"):
                self.wfile.write(b"221 bye\r\n")
                return
            if upper.startswith("RCPT TO"):
                rcpts.append(command.split(":", 1)[1].strip(" <>"))
                self.wfile.write(b"250 ok\r\n")
            elif upper.startswith("DATA"):
                self.wfile.write(b"354 go\r\n")
                collected: list[str] = []
                while True:
                    data_line = self.rfile.readline()
                    if not data_line or data_line.rstrip(b"\r\n") == b".":
                        break
                    collected.append(data_line.decode("utf-8", "replace"))
                self.server.messages.append((list(rcpts), "".join(collected)))
                rcpts = []
                self.wfile.write(b"250 ok\r\n")
            else:  # EHLO/HELO/MAIL FROM/RSET/NOOP — a plain 250 satisfies smtplib
                self.wfile.write(b"250 ok\r\n")


class TelegramStub(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []  # (path, body)
        self.gate: threading.Event | None = None  # scenario 8: hold the response until set
        super().__init__(("127.0.0.1", 0), _TelegramHandler)


class _TelegramHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        assert isinstance(self.server, TelegramStub)
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = json.loads(self.rfile.read(length) or b"{}")
        gate = self.server.gate
        if gate is not None:
            assert gate.wait(timeout=10), "telegram gate never opened — respond-before-notify broken?"
        self.server.records.append((self.path, body))
        payload = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


# --- helpers -----------------------------------------------------------------


def sign(raw: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()


def post(port: int, raw: bytes, signature: str, timeout: float = 10.0) -> int:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/",
        data=raw,
        headers={"Content-Type": "application/json", "X-Atlas-Signature": signature},
        method="POST",
    )
    try:
        # Loopback-only: the URL is built from a 127.0.0.1 ephemeral port above.
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)


def wait_until(condition: Any, what: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def body(delivery_id: str, node_key: str = "dept_head_approval", level: int = 1,
         workflow_name: str = "Purchase approval", event: str = "approval_overdue",
         extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event": event,
        "delivery_id": delivery_id,
        "approval": {"id": "apr_1", "label": "Approve purchase", "reason": "250,000 THB",
                     "choices": [{"id": "approve", "label": "Approve"}],
                     "created_at": "2026-08-07T02:00:00Z", "age_hours": 130.5,
                     "level": level, "threshold_hours": 120},
        "run": {"id": "wfr_1", "node_key": node_key, "workflow_definition_id": "wfd_1",
                "workflow_name": workflow_name},
        "signed_at": "2026-08-12T12:30:00Z",
    }
    if extra:
        payload.update(extra)
    return payload


def canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def outcomes_of(db_path: Path, delivery_id: str) -> dict[str, str]:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT outcomes FROM handled WHERE delivery_id = ?", (delivery_id,)).fetchone()
        return json.loads(row[0]) if row else {}
    finally:
        conn.close()


def closed_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def main() -> None:
    captured: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    LOGGER.setLevel(logging.INFO)
    LOGGER.addHandler(_Capture())

    with TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        smtp = FakeSMTP()
        telegram = TelegramStub()
        threading.Thread(target=smtp.serve_forever, daemon=True).start()
        threading.Thread(target=telegram.serve_forever, daemon=True).start()
        smtp_port = smtp.server_address[1]
        tg_port = telegram.server_address[1]

        # scenario 0: config hygiene — a secret-looking key is refused at load
        bad = tmpdir / "bad.json"
        bad.write_text(json.dumps({"channels": {"telegram": {"api_base": "https://x", "bot_token": "nope"}},
                                   "routes": {}}), encoding="utf-8")
        try:
            load_config(bad)
            raise AssertionError("config with a token key was accepted")
        except SystemExit as exc:
            assert "secret" in str(exc), f"unexpected refusal message: {exc}"

        config_path = tmpdir / "notify.json"
        config_path.write_text(json.dumps({
            "channels": {"smtp": {"host": "127.0.0.1", "port": smtp_port, "starttls": False,
                                  "from": "atlas-notify@check"},
                         "telegram": {"api_base": f"http://127.0.0.1:{tg_port}"}},
            "routes": {"dept_head_approval": {"1": [{"channel": "smtp", "to": "lvl1@check"}],
                                              "2": [{"channel": "smtp", "to": "lvl2@check"}]},
                       "finance_approval": {"1": [{"channel": "telegram", "chat_id": "42"}]}},
            "fallback": [{"channel": "smtp", "to": "ops@check"}],
        }), encoding="utf-8")
        db_path = tmpdir / "notify.sqlite"
        store = Store(db_path)
        notifier = Notifier(load_config(config_path), store, SECRET, None, TELEGRAM_TOKEN)
        server = NotifyServer(("127.0.0.1", 0), notifier)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()

        # scenario 1: wrong signature -> 401, nothing sent, no row
        raw = canonical(body("dlv_apr_apr_1_l1"))
        assert post(port, raw, "sha256=" + "0" * 64) == 401
        assert not smtp.messages and not telegram.records
        assert store.count() == 0, "a rejected delivery must not claim a row"

        # scenario 2: valid signature over NON-canonical bytes -> 204 + notification
        # (kills the re-serialize-then-verify mutant: these bytes != json.dumps(json.loads(raw)))
        pretty = json.dumps(body("dlv_apr_apr_1_l1"), indent=2).encode("utf-8")
        assert post(port, pretty, sign(pretty)) == 204
        wait_until(lambda: len(smtp.messages) == 1, "smtp message for scenario 2")
        rcpts, data = smtp.messages[0]
        assert rcpts == ["lvl1@check"], f"level-1 route went to {rcpts}"

        # scenario 7: content — subject and workflow name reach the channel
        assert "Purchase approval" in data and "Reminder" in data, "composed message missing from SMTP DATA"

        # scenario 3: unknown event and test probe -> 204, nothing sent, no new row
        rows_before = store.count()
        unknown = canonical(body("dlv_x_1", event="quota_alert"))
        assert post(port, unknown, sign(unknown)) == 204
        probe = canonical(body("dlv_apr_test_1", extra={"test": True}))
        assert post(port, probe, sign(probe)) == 204
        time.sleep(0.2)
        assert len(smtp.messages) == 1 and not telegram.records, "unknown event or test probe notified someone"
        assert store.count() == rows_before, "unknown event or test probe claimed a row"

        # scenario 4: duplicate delivery_id -> exactly one notification, then a RESTART
        # (new Store on the same file) must still dedupe
        dup = canonical(body("dlv_apr_apr_1_l1"))
        assert post(port, dup, sign(dup)) == 204
        time.sleep(0.2)
        assert len(smtp.messages) == 1, "duplicate delivery_id notified twice"
        server.notifier = Notifier(load_config(config_path), Store(db_path), SECRET, None, TELEGRAM_TOKEN)
        assert post(port, dup, sign(dup)) == 204
        time.sleep(0.2)
        assert len(smtp.messages) == 1, "dedup did not survive a restart (in-memory store?)"

        # scenario 5: routing — level 3 walks down to level 2; unknown node_key -> fallback
        lvl3 = canonical(body("dlv_apr_apr_1_l3", level=3))
        assert post(port, lvl3, sign(lvl3)) == 204
        wait_until(lambda: len(smtp.messages) == 2, "walk-down smtp message")
        assert smtp.messages[1][0] == ["lvl2@check"], f"level-3 walk-down went to {smtp.messages[1][0]}"
        stray = canonical(body("dlv_apr_apr_2_l1", node_key="never_configured"))
        assert post(port, stray, sign(stray)) == 204
        wait_until(lambda: len(smtp.messages) == 3, "fallback smtp message")
        assert smtp.messages[2][0] == ["ops@check"], f"fallback went to {smtp.messages[2][0]}"

        # scenario 9: CWE-117 — the workflow name flows into the logged subject, so a
        # newline planted there must be escaped in logs; the token must never be logged
        hostile = canonical(body("dlv_apr_apr_1_l9", level=9,
                                 workflow_name="Purchase\nFAKE-LOG-LINE injected"))
        assert post(port, hostile, sign(hostile)) == 204
        wait_until(lambda: len(smtp.messages) == 4, "hostile-name smtp message")
        log_text = "\n".join(captured)
        assert "Purchase\\nFAKE-LOG-LINE" in log_text, "log line did not escape the injected newline"
        assert "\nFAKE-LOG-LINE" not in log_text, "injected newline reached the log verbatim"
        assert TELEGRAM_TOKEN not in log_text, "telegram token leaked into logs"
        # ...and the subject is an email HEADER: the injected newline must not have become
        # a header line of its own (header injection)
        header_block = smtp.messages[3][1].replace("\r\n", "\n").split("\n\n", 1)[0]
        assert not any(line.startswith("FAKE-LOG-LINE") for line in header_block.splitlines()), \
            "workflow_name newline reached the SMTP headers verbatim (header injection)"

        # scenario 8: fast-2xx ordering — telegram held closed until AFTER the POST returns
        telegram.gate = threading.Event()
        gated = canonical(body("dlv_apr_apr_3_l1", node_key="finance_approval"))
        assert post(port, gated, sign(gated), timeout=5.0) == 204, "204 must not wait for the channel"
        telegram.gate.set()
        wait_until(lambda: len(telegram.records) == 1, "gated telegram record")
        telegram.gate = None
        path, tg_body = telegram.records[0]
        assert path == f"/bot{TELEGRAM_TOKEN}/sendMessage" and tg_body["chat_id"] == "42"
        wait_until(lambda: outcomes_of(db_path, "dlv_apr_apr_3_l1") == {"telegram:42": "sent"},
                   "telegram outcome recorded")

        # scenario 6: channel isolation — dead SMTP must not block the telegram target
        dead_config = tmpdir / "dead.json"
        dead_config.write_text(json.dumps({
            "channels": {"smtp": {"host": "127.0.0.1", "port": closed_port(), "starttls": False,
                                  "from": "atlas-notify@check"},
                         "telegram": {"api_base": f"http://127.0.0.1:{tg_port}"}},
            "routes": {"combo_approval": {"1": [{"channel": "smtp", "to": "dead@check"},
                                                {"channel": "telegram", "chat_id": "77"}]}},
        }), encoding="utf-8")
        isolated = NotifyServer(("127.0.0.1", 0),
                                Notifier(load_config(dead_config), store, SECRET, None, TELEGRAM_TOKEN))
        threading.Thread(target=isolated.serve_forever, daemon=True).start()
        combo = canonical(body("dlv_apr_apr_4_l1", node_key="combo_approval"))
        assert post(isolated.server_address[1], combo, sign(combo)) == 204
        wait_until(lambda: len(telegram.records) == 2, "telegram record despite dead smtp")
        assert telegram.records[1][1]["chat_id"] == "77"
        wait_until(lambda: set(outcomes_of(db_path, "dlv_apr_apr_4_l1")) == {"smtp:dead@check", "telegram:77"},
                   "both outcomes recorded")
        recorded = outcomes_of(db_path, "dlv_apr_apr_4_l1")
        assert recorded["telegram:77"] == "sent" and recorded["smtp:dead@check"].startswith("error:"), recorded

    print("notify check ok")


if __name__ == "__main__":
    main()
