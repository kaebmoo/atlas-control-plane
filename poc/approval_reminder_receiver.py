#!/usr/bin/env python3
"""Reference receiver for Atlas `approval_overdue` deliveries (D2c-2).

Atlas can only do one thing when an approval goes overdue: POST a signed JSON body to a URL.
It has no email, no LINE, no chat integration, and no idea who anyone in your organisation is.
**This file is the missing half** — the thing that turns that POST into a message to a person.
Run it, put its URL in the workflow's `approval_webhook_url` (or `ATLAS_APPROVAL_WEBHOOK_URL`),
and replace `notify()` with your real channel.

    export ATLAS_SECRET_KEY="the same value Atlas signs with"
    python3 poc/approval_reminder_receiver.py --port 9000

Atlas side, once:

    export ATLAS_OUTBOUND_ALLOWLIST="127.0.0.1"     # or the host this runs on
    export ATLAS_SECRET_KEY="the same value"        # Atlas refuses to send unsigned
    export ATLAS_APPROVAL_OVERDUE_HOURS="72,168"    # or set it per workflow in the UI

Deliberately stdlib-only and single-file, matching this repository's rules. Everything here is
the *contract*; the transport is not sacred — n8n, a Cloud Function, a Next.js route, or a
thClaws-fronted endpoint are all fine as long as they honour the four rules below.

THE FOUR RULES A RECEIVER MUST HONOUR

1. Verify the signature over the RAW REQUEST BYTES. Atlas signs exactly what it puts on the
   wire (`sign_delivery_body`, atlas/outbound.py:133). `json.loads` then `json.dumps` produces
   different bytes and therefore a different digest — the single most common way a receiver
   ends up rejecting every legitimate delivery. Compare with `hmac.compare_digest`.
2. Answer 2xx, and answer FAST. Any other status (or no answer within
   `ATLAS_OUTBOUND_TIMEOUT`, default 10s) counts as a failed attempt; Atlas retries up to
   `ATLAS_OUTBOUND_MAX_ATTEMPTS` (default 5) with a short bounded backoff, then dead-letters the
   delivery as `failed`. Send the notification AFTER answering, never before.
3. Deduplicate on `delivery_id`. It is stable across every retry of the same delivery
   (`dlv_apr_<approval_id>_l<level>`), so a retry after a timeout you actually processed must
   not notify twice.
4. Do the routing. Atlas states a fact — "this approval is 130 hours old, level 2". WHO hears
   about it is your org chart, which Atlas does not have and should not.

WHAT ATLAS SENDS

    POST <your url>
    Content-Type: application/json
    X-Atlas-Signature: sha256=<hex>

    {"event": "approval_overdue",
     "delivery_id": "dlv_apr_apr_123_l2",
     "approval": {"id", "label", "reason", "choices", "created_at",
                  "age_hours", "level", "threshold_hours"},
     "run": {"id", "node_key", "workflow_definition_id", "workflow_name"},
     "signed_at": "..."}

`level` is 1-based: 1 is the first reminder, 2+ are escalations. `run.node_key` is the gate that
is stuck (`dept_head_approval`, `finance_approval`, …) — route on that, not on the workflow name.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOGGER = logging.getLogger("approval-receiver")

# Rule 3. A dict in memory is enough for a reference; a real receiver should persist this, or a
# restart mid-retry re-notifies. Bounded so a long-lived process cannot grow without limit.
# ponytail: in-memory set with a size cap; swap for the store you already run if you need it to
# survive a restart.
_SEEN_LIMIT = 10_000
_seen: dict[str, None] = {}
_seen_lock = threading.Lock()

# Rule 4. The only place that knows your organisation. Everything else in this file is contract.
ROUTES: dict[str, dict[int, str]] = {
    # node_key            level 1 (remind)          level 2+ (escalate)
    "dept_head_approval": {1: "somchai@example.co.th", 2: "director@example.co.th"},
    "finance_approval": {1: "finance@example.co.th", 2: "cfo@example.co.th"},
    "procurement_approval": {1: "procurement@example.co.th", 2: "coo@example.co.th"},
    "legal_approval": {1: "legal@example.co.th", 2: "gc@example.co.th"},
}
FALLBACK_RECIPIENT = "ops@example.co.th"


def already_handled(delivery_id: str) -> bool:
    """True if this exact delivery was processed before (Rule 3)."""
    with _seen_lock:
        if delivery_id in _seen:
            return True
        if len(_seen) >= _SEEN_LIMIT:
            _seen.pop(next(iter(_seen)))
        _seen[delivery_id] = None
        return False


def recipient_for(node_key: str, level: int) -> str:
    ladder = ROUTES.get(node_key) or {}
    # Levels beyond the last configured one keep going to the last recipient rather than falling
    # off the end: a third threshold added in Atlas must not silently stop notifying anyone.
    for step in range(level, 0, -1):
        if step in ladder:
            return ladder[step]
    return FALLBACK_RECIPIENT


def compose(body: dict) -> tuple[str, str]:
    """The human message. Everything it needs is in the body — a receiver never has to call
    Atlas back, which is exactly why it needs no Atlas credential."""
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


def notify(recipient: str, subject: str, message: str) -> None:
    """REPLACE THIS with your real channel — smtplib, the LINE Messaging API, a Slack webhook,
    a thClaws job. It runs AFTER the 2xx response, so a slow channel cannot make Atlas retry
    (Rule 2)."""
    LOGGER.info("notify %s | %s\n%s", recipient, subject, message)


class Handler(BaseHTTPRequestHandler):
    server_version = "AtlasApprovalReceiver/1.0"
    secret_key = ""

    def log_message(self, fmt: str, *args: object) -> None:
        LOGGER.debug(fmt, *args)

    def _respond(self, status: HTTPStatus) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length)

        # Rule 1: verify over the raw bytes, before parsing anything out of them.
        signature = self.headers.get("X-Atlas-Signature", "")
        expected = "sha256=" + hmac.new(self.secret_key.encode("utf-8"), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            # 401 is deliberate: this is not a transient failure, and Atlas retrying will not fix
            # a key mismatch. The delivery dead-letters as `failed` and shows up on the
            # Deliveries page, which is the signal an operator can actually act on.
            LOGGER.warning("rejected an unsigned or wrongly-signed delivery")
            self._respond(HTTPStatus.UNAUTHORIZED)
            return

        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._respond(HTTPStatus.BAD_REQUEST)
            return

        # Rule 2: answer first. Everything below this line is on our own time.
        self._respond(HTTPStatus.NO_CONTENT)

        if body.get("event") != "approval_overdue":
            # Run-completion deliveries land here too if the same URL is used for both. Ignoring
            # an unknown event is correct: Atlas may add more, and a 4xx would dead-letter them.
            LOGGER.info("ignoring event %r", body.get("event"))
            return

        delivery_id = str(body.get("delivery_id") or "")
        if delivery_id and already_handled(delivery_id):
            LOGGER.info("duplicate delivery %s ignored", delivery_id)
            return

        approval = body.get("approval") or {}
        run = body.get("run") or {}
        recipient = recipient_for(str(run.get("node_key") or ""), int(approval.get("level") or 1))
        subject, message = compose(body)
        try:
            notify(recipient, subject, message)
        except Exception:  # noqa: BLE001 - a channel failure must not kill the server thread
            LOGGER.exception("notification failed for delivery %s", delivery_id)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--path", default="/atlas/approval-overdue", help="informational; every POST is accepted")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    secret = os.getenv("ATLAS_SECRET_KEY") or ""
    if not secret:
        raise SystemExit("ATLAS_SECRET_KEY must be set to the same value Atlas signs with.")
    Handler.secret_key = secret

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    LOGGER.info("listening on http://%s:%d%s", args.host, args.port, args.path)
    LOGGER.info("set the workflow's approval_webhook_url to that URL, and allowlist %s in Atlas", args.host)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
