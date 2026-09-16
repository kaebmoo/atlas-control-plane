"""Atlas Notify — the deployable receiver for Atlas `approval_overdue` deliveries.

`poc/approval_reminder_receiver.py` teaches the contract; this is the artifact an
operator actually runs. Same four rules (verify the signature over the RAW request
bytes; answer 2xx fast and notify after; deduplicate on `delivery_id`; do the
routing yourself), plus what the POC deliberately leaves out:

- durable dedup: an SQLite `handled` table survives a restart mid-retry, and
  records the per-channel outcome of every delivery for forensics;
- real channels: SMTP (smtplib) and Telegram (Bot API), fanning out per target
  independently — one channel failing never blocks another;
- routing from a JSON config file (`--config` / `ATLAS_NOTIFY_CONFIG`) instead of
  a dict in source. Secrets NEVER live in that file: the startup refuses a config
  containing password/token/secret keys. They come from the environment —
  `ATLAS_SECRET_KEY` (shared HMAC key, required), `ATLAS_NOTIFY_SMTP_PASSWORD`
  (iff smtp.username is set), `ATLAS_NOTIFY_TELEGRAM_TOKEN` (iff a telegram
  target exists).

Managed or BYO is an operating decision, not a code one: NT runs this next to a
customer's silo, or the customer runs it themselves — same artifact, same
`approval_overdue` contract v1 (API reference §14: additive-only; a breaking
change would arrive as a new event name, so this receiver branches on `event`
and ignores anything it does not recognise).

Config shape (levels are the workflow's escalation ladder positions, 1-based):

    {"channels": {"smtp": {"host": "smtp.example.co.th", "port": 587,
                            "starttls": true, "from": "atlas-notify@example.co.th",
                            "username": "atlas-notify"},
                  "telegram": {"api_base": "https://api.telegram.org"}},
     "routes": {"dept_head_approval": {
                    "1": [{"channel": "smtp", "to": "somchai@example.co.th"}],
                    "2": [{"channel": "smtp", "to": "director@example.co.th"},
                          {"channel": "telegram", "chat_id": "-100123456"}]}},
     "fallback": [{"channel": "smtp", "to": "ops@example.co.th"}]}

Levels beyond the last configured one keep notifying the last configured level
(a third threshold added in Atlas must not silently stop notifying anyone), and
an unrouted `node_key` falls back to `fallback`.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import smtplib
import sqlite3
import urllib.request
from datetime import UTC, datetime
from email.message import EmailMessage
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("atlas-notify")

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path(os.getenv("ATLAS_NOTIFY_DB", ROOT / "data" / "notify.sqlite"))
CHANNEL_TIMEOUT = 15  # seconds per channel send; runs after the 2xx, so it never stalls Atlas

_SCHEMA = """
CREATE TABLE IF NOT EXISTS handled (
  delivery_id TEXT PRIMARY KEY,
  event TEXT NOT NULL,
  received_at TEXT NOT NULL,
  outcomes TEXT NOT NULL DEFAULT '{}'
);
"""


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _one_line(value: str, limit: int = 500) -> str:
    """Collapse anything bound for a log line into one line (CWE-117): a gate label or
    reason comes from whoever authored the workflow, and a newline in either would let
    them write what looks like a second log entry."""
    escaped = value.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return escaped if len(escaped) <= limit else escaped[: limit - 1] + "…"


class Store:
    """Durable dedup + per-channel outcome log. `INSERT OR IGNORE` on the primary key is
    the atomic claim: exactly one winner per delivery_id, restarts included — the reason
    this exists instead of the POC's in-memory dict."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            with conn:
                conn.executescript(_SCHEMA)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def claim(self, delivery_id: str, event: str) -> bool:
        """True exactly once per delivery_id, durably."""
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO handled (delivery_id, event, received_at) VALUES (?, ?, ?)",
                    (delivery_id, event, now_iso()),
                )
            return cur.rowcount == 1
        finally:
            conn.close()

    def record(self, delivery_id: str, outcomes: dict[str, str]) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE handled SET outcomes = ? WHERE delivery_id = ?",
                    (json.dumps(outcomes, sort_keys=True), delivery_id),
                )
        finally:
            conn.close()

    def count(self) -> int:
        conn = self._connect()
        try:
            return int(conn.execute("SELECT COUNT(*) FROM handled").fetchone()[0])
        finally:
            conn.close()


_SECRET_KEY_WORDS = ("password", "token", "secret")


def _find_secret_key(node: Any, path: str = "config") -> str | None:
    """Path of the first config key that looks like a secret, or None. Secrets belong in
    the environment; a config file gets copied, committed, and backed up."""
    if isinstance(node, dict):
        for key, value in node.items():
            if any(word in str(key).lower() for word in _SECRET_KEY_WORDS):
                return f"{path}.{key}"
            found = _find_secret_key(value, f"{path}.{key}")
            if found:
                return found
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found = _find_secret_key(item, f"{path}[{index}]")
            if found:
                return found
    return None


def _validate_target(target: Any, where: str, channels: dict[str, Any]) -> None:
    if not isinstance(target, dict):
        raise SystemExit(f"config: {where} must be an object")
    channel = target.get("channel")
    if channel not in ("smtp", "telegram"):
        raise SystemExit(f"config: {where} has unknown channel {channel!r} (smtp or telegram)")
    if channel not in channels:
        raise SystemExit(f"config: {where} uses channel {channel!r} but channels.{channel} is not configured")
    if channel == "smtp" and not target.get("to"):
        raise SystemExit(f"config: {where} is an smtp target without 'to'")
    if channel == "telegram" and not target.get("chat_id"):
        raise SystemExit(f"config: {where} is a telegram target without 'chat_id'")


def load_config(path: Path) -> dict[str, Any]:
    """Parse + validate fail-closed: a config error at startup, never at 03:00 when the
    first escalation fires. Returns routes with int level keys."""
    try:
        parsed = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"config: cannot read {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SystemExit("config: top level must be an object")
    secret_path = _find_secret_key(parsed)
    if secret_path:
        raise SystemExit(
            f"config: {secret_path} looks like a secret — secrets live in the environment "
            "(ATLAS_NOTIFY_SMTP_PASSWORD / ATLAS_NOTIFY_TELEGRAM_TOKEN), never in the config file"
        )
    channels = parsed.get("channels")
    if not isinstance(channels, dict) or not channels:
        raise SystemExit("config: channels must be a non-empty object")
    smtp = channels.get("smtp")
    if smtp is not None:
        if not isinstance(smtp, dict) or not smtp.get("host") or not smtp.get("from"):
            raise SystemExit("config: channels.smtp needs at least 'host' and 'from'")
    telegram = channels.get("telegram")
    if telegram is not None:
        if not isinstance(telegram, dict):
            raise SystemExit("config: channels.telegram must be an object")
        api_base = str(telegram.get("api_base") or "https://api.telegram.org")
        if not api_base.startswith(("http://", "https://")):
            raise SystemExit(f"config: channels.telegram.api_base has unsupported scheme: {api_base}")
        telegram["api_base"] = api_base
    routes_in = parsed.get("routes")
    if not isinstance(routes_in, dict):
        raise SystemExit("config: routes must be an object (node_key -> level -> targets)")
    routes: dict[str, dict[int, list[dict[str, Any]]]] = {}
    for node_key, ladder in routes_in.items():
        if not isinstance(ladder, dict) or not ladder:
            raise SystemExit(f"config: routes.{node_key} must be a non-empty object of levels")
        routes[str(node_key)] = {}
        for level_key, targets in ladder.items():
            try:
                level = int(level_key)
            except (TypeError, ValueError):
                raise SystemExit(f"config: routes.{node_key} level {level_key!r} is not an integer") from None
            if level < 1:
                raise SystemExit(f"config: routes.{node_key} level {level} must be >= 1 (levels are 1-based)")
            if not isinstance(targets, list) or not targets:
                raise SystemExit(f"config: routes.{node_key}.{level_key} must be a non-empty list of targets")
            for index, target in enumerate(targets):
                _validate_target(target, f"routes.{node_key}.{level_key}[{index}]", channels)
            routes[str(node_key)][level] = targets
    fallback = parsed.get("fallback") or []
    if not isinstance(fallback, list):
        raise SystemExit("config: fallback must be a list of targets")
    for index, target in enumerate(fallback):
        _validate_target(target, f"fallback[{index}]", channels)
    if not fallback:
        LOGGER.warning("config has no fallback — deliveries for unrouted node_keys will be dropped (recorded, not notified)")
    return {"channels": channels, "routes": routes, "fallback": fallback}


def targets_for(config: dict[str, Any], node_key: str, level: int) -> list[dict[str, Any]]:
    """Walk down from the delivery's level to the last configured one (same semantics as
    the POC's recipient_for: a new threshold in Atlas must not silently notify nobody),
    else the global fallback."""
    ladder: dict[int, list[dict[str, Any]]] = config["routes"].get(node_key) or {}
    for step in range(level, 0, -1):
        if step in ladder:
            return ladder[step]
    return list(config.get("fallback") or [])


def describe_target(target: dict[str, Any]) -> str:
    if target.get("channel") == "telegram":
        return f"telegram:{target.get('chat_id')}"
    return f"smtp:{target.get('to')}"


def compose(body: dict[str, Any]) -> tuple[str, str]:
    """The human message. Everything it needs is in the body — a receiver never calls
    Atlas back, which is exactly why it needs no Atlas credential. (Lifted from the POC.)"""
    approval = body.get("approval") or {}
    run = body.get("run") or {}
    level = int(approval.get("level") or 1)
    age_days = round(float(approval.get("age_hours") or 0) / 24, 1)
    verb = "Reminder" if level == 1 else "ESCALATION"
    subject = f"{verb}: approval pending {age_days} days — {run.get('workflow_name') or 'workflow'}"
    lines = [
        f"{approval.get('label') or 'An approval'} is still waiting.",
        "",
        f"  Workflow : {run.get('workflow_name')}",
        f"  Step     : {run.get('node_key')}",
        f"  Waiting  : {age_days} days (threshold {approval.get('threshold_hours')}h, level {level})",
        f"  Reason   : {approval.get('reason') or '—'}",
        "",
        f"  Approval : {approval.get('id')}",
        f"  Run      : {run.get('id')}",
    ]
    choices = approval.get("choices") or []
    if choices:
        lines.append("  Options  : " + ", ".join(str(choice.get("label") or choice.get("id")) for choice in choices))
    return subject, "\n".join(lines)


def send_smtp(channel: dict[str, Any], password: str | None, to: str, subject: str, message: str) -> None:
    mail = EmailMessage()
    # The subject becomes an email HEADER, and workflow_name (author-controlled) flows into
    # it — an unescaped newline there is header injection, so collapse it here, at the
    # boundary where it matters. The body keeps its newlines; they are content.
    mail["Subject"] = _one_line(subject)
    mail["From"] = channel["from"]
    mail["To"] = to
    mail.set_content(message)
    with smtplib.SMTP(str(channel["host"]), int(channel.get("port", 587)), timeout=CHANNEL_TIMEOUT) as smtp:
        if channel.get("starttls"):
            smtp.starttls()
        username = channel.get("username")
        if username:
            smtp.login(str(username), password or "")
        smtp.send_message(mail)


def send_telegram(channel: dict[str, Any], bot_token: str, chat_id: str, subject: str, message: str) -> None:
    url = f"{str(channel.get('api_base', 'https://api.telegram.org')).rstrip('/')}/bot{bot_token}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": f"{subject}\n\n{message}"}).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    # api_base is operator-owned config with the scheme validated http(s) at load — not request data.
    with urllib.request.urlopen(request, timeout=CHANNEL_TIMEOUT) as response:  # nosec B310
        response.read()


class Notifier:
    """Everything after the 204: event branch, durable claim, routing, compose, fan-out."""

    def __init__(
        self,
        config: dict[str, Any],
        store: Store,
        secret_key: str,
        smtp_password: str | None = None,
        telegram_token: str | None = None,
    ):
        self.config = config
        self.store = store
        self.secret_key = secret_key
        self.smtp_password = smtp_password
        self.telegram_token = telegram_token

    def signature_ok(self, raw: bytes, header: str) -> bool:
        expected = "sha256=" + hmac.new(self.secret_key.encode("utf-8"), raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(header, expected)

    def send(self, target: dict[str, Any], subject: str, message: str) -> None:
        if target["channel"] == "telegram":
            send_telegram(self.config["channels"]["telegram"], self.telegram_token or "",
                          str(target["chat_id"]), subject, message)
        else:
            send_smtp(self.config["channels"]["smtp"], self.smtp_password,
                      str(target["to"]), subject, message)

    def handle(self, body: dict[str, Any]) -> None:
        """Runs AFTER the 2xx response, on the request thread — a slow or dead channel
        can never make Atlas count a failed attempt."""
        if body.get("event") != "approval_overdue":
            # Run-completion deliveries land here too if the same URL serves both, and
            # Atlas may add event names (contract: additive by NEW event name) — ignoring
            # the unrecognised is correct, a 4xx would dead-letter them.
            LOGGER.info("ignoring event %r", body.get("event"))
            return
        if body.get("test"):
            LOGGER.info("test probe verified ok — not notifying anyone")
            return
        delivery_id = str(body.get("delivery_id") or "")
        if not delivery_id:
            LOGGER.warning("approval_overdue without delivery_id ignored")
            return
        if not self.store.claim(delivery_id, "approval_overdue"):
            LOGGER.info("duplicate delivery %s ignored", _one_line(delivery_id))
            return
        approval = body.get("approval") or {}
        run = body.get("run") or {}
        node_key = str(run.get("node_key") or "")
        level = int(approval.get("level") or 1)
        targets = targets_for(self.config, node_key, level)
        if not targets:
            LOGGER.warning("no route and no fallback for node_key %s level %d — delivery %s dropped",
                           _one_line(node_key), level, _one_line(delivery_id))
            self.store.record(delivery_id, {"route": "no targets"})
            return
        subject, message = compose(body)
        outcomes: dict[str, str] = {}
        for target in targets:
            label = describe_target(target)
            try:
                self.send(target, subject, message)
                outcomes[label] = "sent"
                LOGGER.info("notified %s | %s | %s", _one_line(label), _one_line(delivery_id), _one_line(subject))
            except Exception as exc:  # noqa: BLE001 - one channel failing must not block the others
                outcomes[label] = f"error: {exc}"
                LOGGER.error("channel %s failed for %s: %s",
                             _one_line(label), _one_line(delivery_id), _one_line(str(exc)))
        self.store.record(delivery_id, outcomes)


class NotifyServer(ThreadingHTTPServer):
    """Carries the Notifier so several servers (and configs) can coexist in one process —
    which is exactly what the hermetic check does."""

    def __init__(self, address: tuple[str, int], notifier: Notifier):
        super().__init__(address, Handler)
        self.notifier = notifier


class Handler(BaseHTTPRequestHandler):
    server_version = "AtlasNotify/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        LOGGER.debug(fmt, *args)

    def _respond(self, status: HTTPStatus) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        assert isinstance(self.server, NotifyServer)
        notifier = self.server.notifier
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length)

        # Rule 1: verify over the raw bytes, before parsing anything out of them.
        if not notifier.signature_ok(raw, self.headers.get("X-Atlas-Signature", "")):
            # 401 on purpose: a key mismatch is not transient, retrying cannot fix it, and
            # the delivery dead-letters as `failed` where an operator can see it.
            LOGGER.warning("rejected an unsigned or wrongly-signed delivery")
            self._respond(HTTPStatus.UNAUTHORIZED)
            return
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._respond(HTTPStatus.BAD_REQUEST)
            return

        # Rule 2: answer first. Everything below is on our own time.
        self._respond(HTTPStatus.NO_CONTENT)
        try:
            notifier.handle(body if isinstance(body, dict) else {})
        except Exception:  # noqa: BLE001 - a handling failure must not kill the server thread
            LOGGER.exception("delivery handling failed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--config", default=os.getenv("ATLAS_NOTIFY_CONFIG") or "",
                        help="path to the routes/channels JSON (or ATLAS_NOTIFY_CONFIG)")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite dedup store (or ATLAS_NOTIFY_DB)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not args.config:
        raise SystemExit("--config (or ATLAS_NOTIFY_CONFIG) is required")
    secret_key = os.getenv("ATLAS_SECRET_KEY") or ""
    if not secret_key:
        raise SystemExit("ATLAS_SECRET_KEY must be set to the same value Atlas signs with.")
    config = load_config(Path(args.config))

    smtp_channel = config["channels"].get("smtp") or {}
    smtp_password = os.getenv("ATLAS_NOTIFY_SMTP_PASSWORD")
    if smtp_channel.get("username") and not smtp_password:
        raise SystemExit("channels.smtp.username is set, so ATLAS_NOTIFY_SMTP_PASSWORD must be too")
    all_targets = [t for ladder in config["routes"].values() for targets in ladder.values() for t in targets]
    all_targets += config["fallback"]
    telegram_token = os.getenv("ATLAS_NOTIFY_TELEGRAM_TOKEN")
    if any(t["channel"] == "telegram" for t in all_targets) and not telegram_token:
        raise SystemExit("a telegram target exists, so ATLAS_NOTIFY_TELEGRAM_TOKEN must be set")

    store = Store(Path(args.db))
    notifier = Notifier(config, store, secret_key, smtp_password, telegram_token)
    server = NotifyServer((args.host, args.port), notifier)
    # Plain http on purpose: Atlas refuses to POST http to any non-loopback host, so a
    # receiver exposed off-box terminates TLS in front of itself (a deployment concern).
    LOGGER.info("listening on http://%s:%d", args.host, args.port)  # NOSONAR S5332
    LOGGER.info("set approval_webhook_url to that URL, and allowlist %s in ATLAS_OUTBOUND_ALLOWLIST", args.host)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
