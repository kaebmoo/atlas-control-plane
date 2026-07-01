from __future__ import annotations

import contextlib
import hashlib
import hmac
import http.client
import ipaddress
import json
import socket
import ssl
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from .db import Database, now_iso


@dataclass(frozen=True)
class OutboundTarget:
    """Result of validating a callback_url against ATLAS_OUTBOUND_ALLOWLIST. `allowed=False`
    always carries a human-readable `reason`; IA-1 uses that to reject an envelope pre-run, OB-1
    uses it to mark a delivery `blocked` instead of connecting anywhere."""

    allowed: bool
    reason: str = ""
    hostname: str | None = None
    port: int | None = None
    scheme: str | None = None
    path: str = "/"
    # Set whenever allowed=True (whether the host matched the allowlist by exact name or by a
    # resolved address falling inside an allowlisted CIDR): the sender pins the TCP connection
    # to exactly this address so a DNS answer that changes between this check and the actual
    # send (rebinding) can't redirect the request past the guard.
    pinned_ip: str | None = None


def _is_loopback_literal(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _split_allowlist(allowlist: tuple[str, ...]) -> tuple[set[str], list[ipaddress.IPv4Network | ipaddress.IPv6Network]]:
    hosts: set[str] = set()
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw in allowlist:
        entry = raw.strip()
        if not entry:
            continue
        hosts.add(entry.lower())
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            pass  # a bare hostname (e.g. "relay.internal.nt.th") never parses as a network
    return hosts, networks


def _url_shape_reason(parsed: Any) -> str | None:
    """Reject a callback_url that carries data in the parts where a secret would hide, instead
    of chasing credential-shaped keywords (a denylist any `webhook_secret`/`code`/`sig` slips
    past). A webhook callback is an ADDRESS: it needs scheme+host+path, nothing else. Userinfo,
    a query string, or a fragment are the three places a token gets smuggled — and they'd then
    be persisted into run input and echoed back through read APIs. Structurally forbidding all
    three ends the class. Adapters authenticate the receiver with X-Atlas-Signature, and route
    per-user context with `_meta.reply.correlation_id` (echoed in the delivery), not the URL."""
    if parsed.username or parsed.password:
        return "callback_url must not embed credentials (no user:pass@host); authenticate with X-Atlas-Signature"
    if parsed.query:
        return "callback_url must not contain a query string; use _meta.reply.correlation_id for routing context"
    if parsed.fragment:
        return "callback_url must not contain a URL fragment"
    return None


def _resolve_ips(hostname: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except OSError:
        return []
    # dict.fromkeys: de-dupe while keeping getaddrinfo's ordering (used to pick a stable pin).
    # sockaddr[0] (the address) is always a str at runtime; typeshed just types it as str|int
    # because the tuple shape is shared with non-address socket families.
    return list(dict.fromkeys(str(info[4][0]).split("%", 1)[0] for info in infos))


def resolve_outbound_target(url: str, allowlist: tuple[str, ...]) -> OutboundTarget:
    """Validate a reply callback_url: syntactically valid http(s) URL, https unless the host is
    loopback (dev convenience), and the host itself allowlisted by ATLAS_OUTBOUND_ALLOWLIST —
    either by exact hostname match (operator-trusted relay) or because every address it resolves
    to falls inside an allowlisted CIDR. An EMPTY allowlist always blocks (outbound disabled by
    default). Shared by IA-1 (reject an undeliverable reply at ingress) and OB-1 (guard the actual
    send) so the two never drift apart."""
    if not allowlist:
        return OutboundTarget(False, "outbound delivery is disabled (ATLAS_OUTBOUND_ALLOWLIST is empty)")
    try:
        parsed = urlparse(url)
    except ValueError:
        return OutboundTarget(False, "callback_url is not a valid URL")
    hostname = parsed.hostname
    if not hostname or parsed.scheme not in {"http", "https"}:
        return OutboundTarget(False, "callback_url must be an http(s) URL")
    shape_reason = _url_shape_reason(parsed)
    if shape_reason:
        return OutboundTarget(False, shape_reason)
    loopback = _is_loopback_literal(hostname)
    if parsed.scheme == "http" and not loopback:
        return OutboundTarget(False, "callback_url must use https (http is only allowed to a loopback host)")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    hosts, networks = _split_allowlist(allowlist)
    by_name = hostname.lower() in hosts
    # Resolve unconditionally (even for a by-name allowlist match) so the sender always has a
    # validated address to pin the actual connection to — not just an operator-trusted name it
    # would otherwise re-resolve at connect time, which is exactly the DNS-rebinding gap between
    # this check and the send.
    resolved = _resolve_ips(hostname, port)
    if not resolved:
        return OutboundTarget(False, f"callback_url host could not be resolved: {hostname}")
    if not by_name:
        for raw_ip in resolved:
            ip = ipaddress.ip_address(raw_ip)
            if not any(ip in network for network in networks):
                return OutboundTarget(False, f"callback_url host is not covered by ATLAS_OUTBOUND_ALLOWLIST: {hostname}")
    return OutboundTarget(True, hostname=hostname, port=port, scheme=parsed.scheme, path=path, pinned_ip=resolved[0])


def sign_delivery_body(secret_key: str, body: bytes) -> str:
    """Same HMAC-SHA256 primitive as the signed usage export (atlas/usage.py), applied to the
    exact bytes sent on the wire (not a re-canonicalized copy) so the receiver can verify
    byte-for-byte."""
    return "sha256=" + hmac.new(secret_key.encode("utf-8"), body, hashlib.sha256).hexdigest()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """An HTTPSConnection whose socket connects to a pre-validated IP instead of re-resolving
    `hostname` at connect time, while still doing normal TLS server-name verification against
    `hostname` — closing the gap between resolve_outbound_target's DNS check and the actual
    send (a rebound DNS answer in between can't redirect the request past the guard)."""

    def __init__(self, hostname: str, pinned_ip: str, port: int, timeout: float):
        super().__init__(hostname, port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        context = ssl.create_default_context()
        self.sock = context.wrap_socket(sock, server_hostname=self.host)


def _drain_response(response: http.client.HTTPResponse, timeout: float, max_bytes: int = 65536) -> None:
    """Read and discard the response body under a bounded byte count AND a total wall-clock
    deadline. `read(n)` on a socket-backed stream loops internally trying to fill the full `n`
    bytes before returning, so a receiver trickling one byte at a time could keep a single call
    blocked for the whole trickle regardless of any deadline checked between calls (the same
    pitfall documented on iter_sse in atlas/thclaws_client.py). `read1(n)` makes at most one
    underlying system call and returns whatever is already available, so the deadline is
    actually checked every time data trickles in — not just once per accumulated 8KiB. The
    connection's own socket timeout still bounds each individual call if the peer goes silent.
    Callers only use the status code, so the body itself is discarded either way."""
    deadline = time.monotonic() + timeout
    read_chunk = getattr(response, "read1", None) or response.read
    read = 0
    while read < max_bytes and time.monotonic() < deadline:
        try:
            chunk = read_chunk(min(8192, max_bytes - read))
        except (TimeoutError, OSError):
            return
        if not chunk:
            return
        read += len(chunk)


def _send(target: OutboundTarget, body: bytes, headers: dict[str, str], timeout: float) -> int:
    assert target.hostname and target.port and target.scheme and target.pinned_ip
    conn: http.client.HTTPConnection
    if target.scheme == "https":
        conn = _PinnedHTTPSConnection(target.hostname, target.pinned_ip, target.port, timeout)
    else:
        # ponytail: plain-http only ever reaches a loopback host (enforced above) — connecting
        # by the pinned address directly is simplest, no SNI/cert concern to preserve hostname for.
        conn = http.client.HTTPConnection(target.pinned_ip, target.port, timeout=timeout)
    # Hard TOTAL wall-clock ceiling for the whole exchange. The socket timeout is only a
    # per-recv INACTIVITY timeout, so a receiver trickling status-line/header bytes just under
    # it resets the clock on every byte and never trips — the deadline check in _drain_response
    # covers only the body, after the status is already read. A watchdog shuts the socket down
    # at `timeout` to bound connect + request + response headers + body together. It uses
    # shutdown(), not close(): getresponse() hands the socket to an HTTPResponse whose file
    # object keeps the fd open, so a plain close() would not interrupt an in-flight header read
    # — shutdown(SHUT_RDWR) forces the blocked recv to return at the OS level.
    deadline = time.monotonic() + timeout
    aborted = threading.Event()

    def _fire() -> None:
        aborted.set()
        _abort_connection(conn)

    watchdog = threading.Timer(timeout, _fire)
    watchdog.daemon = True
    watchdog.start()
    try:
        conn.request("POST", target.path, body=body, headers=headers)
        response = conn.getresponse()
        # If the watchdog fired while reading the status/headers, http.client may still salvage
        # a lenient status (e.g. a truncated "HTTP/1.1 200" line at EOF parses as 200). That is
        # NOT a delivery — we never received a complete response within the deadline. Treat it
        # as a timeout (TimeoutError is an OSError subclass, so _attempt records a failed
        # attempt). A slow BODY does not hit this: its status+headers were read before the
        # deadline, so `aborted` is still clear here and only the (discarded) body drain is cut.
        if aborted.is_set():
            raise TimeoutError(f"delivery exceeded its {timeout:g}s deadline before a response was received")
        status = response.status
        _drain_response(response, max(0.0, deadline - time.monotonic()))
        return status
    finally:
        watchdog.cancel()
        conn.close()


def _abort_connection(conn: http.client.HTTPConnection) -> None:
    sock = conn.sock
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    conn.close()


@dataclass(frozen=True)
class OutboundSettings:
    allowlist: tuple[str, ...] = ()
    secret_key: str | None = None
    max_attempts: int = 5
    timeout_seconds: float = 10.0


def _artifact_payload(artifact: dict[str, Any]) -> dict[str, Any]:
    content = artifact.get("content")
    if artifact.get("kind") == "json" and isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            pass
    return {"key": artifact.get("key"), "kind": artifact.get("kind"), "content": content}


# Fixed, short, bounded backoff between automatic delivery attempts (External Decision #4 in
# the plan: revisit if a real delivery SLA needs slower/longer backoff). Deliberately small so
# a hermetic check exercising the full retry-to-failed path stays fast.
_BACKOFF_BASE_SECONDS = 0.05
_BACKOFF_CAP_SECONDS = 1.0


class OutboundService:
    """OB-1: signed outbound delivery of a completed run's result to `_meta.reply.callback_url`.
    A failure-isolated side effect (mirrors usage metering): every entry point here is called
    only after the run's outcome is already persisted, and nothing in this class ever writes
    back to workflow_runs — a delivery failure can never change a run's state."""

    def __init__(self, db: Database, settings: OutboundSettings):
        self.db = db
        self.settings = settings
        # Delivery ids with an attempt loop currently running. claim_delivery() dedupes row
        # CREATION, but two senders (a live completion vs the startup reconcile scan, or a
        # manual retry) could still both drive the SAME row: the loser's failure would then
        # overwrite the winner's `delivered`. Every sender lives in this process (no attempt
        # thread survives a restart — that is what reconcile is for), so an in-process claim
        # is sufficient; a DB lease would only matter if delivery ever went multi-process.
        self._inflight: set[str] = set()
        self._inflight_lock = threading.Lock()

    @contextlib.contextmanager
    def _claimed(self, delivery_id: str) -> Iterator[bool]:
        """Yield True iff this caller now exclusively owns attempts for delivery_id."""
        with self._inflight_lock:
            if delivery_id in self._inflight:
                yield False
                return
            self._inflight.add(delivery_id)
        try:
            yield True
        finally:
            with self._inflight_lock:
                self._inflight.discard(delivery_id)

    def _drive_owned(self, delivery: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
        """Claim ownership, RE-READ the row, and only drive it if it is still pending — by the
        time we win the claim the previous owner may already have finished it, and re-sending a
        terminal row is exactly the delivered->failed regression this guard exists to prevent."""
        with self._claimed(delivery["id"]) as owned:
            if not owned:
                return delivery  # someone else is mid-attempt; they will finish it
            current = self.db.get_delivery(delivery["id"]) or delivery
            if current["status"] != "pending":
                return current
            return self._run_to_completion(current, run)

    def deliver_run_completion(self, run: dict[str, Any]) -> dict[str, Any] | None:
        """Call once, right after a run reaches succeeded/failed (from the run's own background
        completion thread) AND from restart reconcile. Both target a deterministic delivery id
        for the run, claimed atomically: whoever wins the insert drives the full bounded-retry
        loop; a loser (e.g. a startup reconcile racing the live completion) returns the existing
        row without opening a second, differently-id'd delivery the receiver couldn't dedupe.
        Returns None if this run's _meta.reply did not request webhook delivery."""
        if run.get("state") not in {"succeeded", "failed"}:
            return None
        reply = _reply_of(run)
        if not reply or reply.get("mode") != "webhook":
            return None
        callback_url = reply.get("callback_url")
        if not isinstance(callback_url, str) or not callback_url:
            return None
        delivery, created = self.db.claim_delivery(
            {
                "id": _completion_delivery_id(run["id"]),
                "run_id": run["id"],
                "url": callback_url,
                "correlation_id": reply.get("correlation_id") if isinstance(reply.get("correlation_id"), str) else None,
                "max_attempts": self.settings.max_attempts,
            }
        )
        if not created:
            return delivery  # another path already owns this run's completion delivery
        return self._drive_owned(delivery, run)

    def deliver_run(self, run: dict[str, Any]) -> dict[str, Any]:
        """POST /api/workflow-runs/{id}/deliver: one manual, immediate (re)send. `mode` need not
        be "webhook" — an explicit manual request overrides the adapter's original poll
        preference, as long as a reply address is configured."""
        if run.get("state") not in {"succeeded", "failed"}:
            raise ValueError("workflow run has not completed yet")
        reply = _reply_of(run)
        callback_url = reply.get("callback_url") if reply else None
        if not isinstance(callback_url, str) or not callback_url:
            raise ValueError("workflow run has no _meta.reply.callback_url configured")
        delivery = self._create_delivery(run["id"], callback_url, reply.get("correlation_id") if reply else None)
        return self._attempt(delivery, run)

    def retry_delivery(self, delivery_id: str) -> dict[str, Any]:
        """POST /api/deliveries/{id}/retry: one bounded manual attempt, re-validating the
        callback_url against the CURRENT allowlist (an operator may have just fixed it)."""
        delivery = self.db.get_delivery(delivery_id)
        if not delivery:
            raise ValueError(f"Unknown delivery_id: {delivery_id}")
        run = self.db.get_workflow_run(delivery["run_id"])
        if not run:
            raise ValueError(f"delivery {delivery_id} has no workflow run")
        with self._claimed(delivery_id) as owned:
            if not owned:
                # Forcing status back to `pending` under a live attempt loop would stomp the
                # outcome that loop is about to write; the operator can retry once it settles.
                raise ValueError(f"delivery {delivery_id} already has an attempt in progress")
            delivery = self.db.update_delivery(delivery_id, status="pending") or delivery
            return self._attempt(delivery, run)

    def _create_delivery(self, run_id: str, callback_url: str, correlation_id: Any) -> dict[str, Any]:
        return self.db.create_delivery(
            {
                "run_id": run_id,
                "url": callback_url,
                "correlation_id": correlation_id if isinstance(correlation_id, str) else None,
                "max_attempts": self.settings.max_attempts,
            }
        )

    def reconcile(self) -> None:
        """Crash/restart recovery — no delivery-attempt thread survives a restart. Complete on
        both axes, keyed off the deliveries/runs tables directly (NOT a capped run scan, so an
        interrupted delivery is recovered no matter how old its run is): (1) every delivery left
        `pending` is re-driven to a terminal state, and (2) every terminal run whose _meta.reply
        asked for webhook delivery but that has NO delivery row (a crash between finalizing the
        run and creating the row) gets one created and driven. Mirrors WorkflowRunner.reconcile_runs
        / JobManager.reconcile_jobs — same "no thread survives, resume from the DB" discipline."""
        for delivery_id in self.db.iter_pending_delivery_ids():
            delivery = self.db.get_delivery(delivery_id)
            if not delivery or delivery["status"] != "pending":
                continue  # a concurrent live attempt may have already moved it
            run = self.db.get_workflow_run(delivery["run_id"])
            if run:
                self._drive_owned(delivery, run)
        for run_id in self.db.runs_missing_webhook_delivery():
            run = self.db.get_workflow_run(run_id)
            if run:
                self.deliver_run_completion(run)

    def _run_to_completion(self, delivery: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
        while True:
            delivery = self._attempt(delivery, run)
            if delivery["status"] != "pending":
                return delivery
            time.sleep(min(_BACKOFF_BASE_SECONDS * (2 ** (delivery["attempts"] - 1)), _BACKOFF_CAP_SECONDS))

    def _attempt(self, delivery: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.secret_key:
            reason = "ATLAS_SECRET_KEY is not configured; refusing to send an unsigned delivery"
            return self._block(delivery, reason)
        target = resolve_outbound_target(delivery["url"], self.settings.allowlist)
        if not target.allowed:
            return self._block(delivery, target.reason)
        body_dict = {
            "delivery_id": delivery["id"],
            "run_id": run["id"],
            "state": run.get("state"),
            "correlation_id": delivery.get("correlation_id"),
            "artifacts": [_artifact_payload(artifact) for artifact in self.db.list_artifacts(run_id=run["id"], limit=1000)],
            "signed_at": now_iso(),
        }
        body = json.dumps(body_dict, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json", "X-Atlas-Signature": sign_delivery_body(self.settings.secret_key, body)}
        attempts = delivery["attempts"] + 1
        try:
            status = _send(target, body, headers, self.settings.timeout_seconds)
            if 200 <= status < 300:
                updated = self.db.update_delivery(
                    delivery["id"], status="delivered", attempts=attempts, last_error=None, delivered_at=now_iso()
                ) or delivery
                self.db.audit("delivery.delivered", "delivery", delivery["id"], {"run_id": run["id"], "attempts": attempts})
                return updated
            error = f"receiver returned HTTP {status}"
        except (OSError, http.client.HTTPException) as exc:
            error = f"{type(exc).__name__}: {exc}"
        next_status = "pending" if attempts < delivery["max_attempts"] else "failed"
        updated = self.db.update_delivery(delivery["id"], status=next_status, attempts=attempts, last_error=error) or delivery
        if next_status == "failed":
            self.db.audit(
                "delivery.failed", "delivery", delivery["id"], {"run_id": run["id"], "attempts": attempts, "last_error": error}
            )
        return updated

    def _block(self, delivery: dict[str, Any], reason: str) -> dict[str, Any]:
        updated = self.db.update_delivery(delivery["id"], status="blocked", last_error=reason) or delivery
        self.db.audit("delivery.blocked", "delivery", delivery["id"], {"run_id": delivery.get("run_id"), "reason": reason})
        return updated


def _completion_delivery_id(run_id: str) -> str:
    """Stable, opaque id for a run's ONE automatic completion delivery, so the live completion
    path and a restart reconcile insert the same row (INSERT OR IGNORE → one wins) and the
    receiver sees a single delivery_id to dedupe. Manual `deliver_run` sends keep random ids —
    an explicit re-send is meant to be a distinct delivery."""
    digest = hashlib.sha256(f"completion:{run_id}".encode("utf-8")).hexdigest()
    return f"dlv_{digest[:16]}"


def _reply_of(run: dict[str, Any]) -> dict[str, Any] | None:
    reply = ((run.get("input") or {}).get("_meta") or {}).get("reply")
    return reply if isinstance(reply, dict) else None
