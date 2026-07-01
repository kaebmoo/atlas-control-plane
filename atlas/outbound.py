from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import socket
import ssl
import time
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
    # None when the host matched the allowlist BY NAME (an operator-trusted relay hostname):
    # the sender then resolves DNS normally at connect time, same as any stdlib HTTP client.
    # Set when the host was allowed by RESOLVING it into an allowlisted CIDR: the sender pins
    # the TCP connection to exactly this address so a DNS answer that changes between this
    # check and the actual send (rebinding) can't redirect the request past the guard.
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
    loopback = _is_loopback_literal(hostname)
    if parsed.scheme == "http" and not loopback:
        return OutboundTarget(False, "callback_url must use https (http is only allowed to a loopback host)")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    hosts, networks = _split_allowlist(allowlist)
    if hostname.lower() in hosts:
        return OutboundTarget(True, hostname=hostname, port=port, scheme=parsed.scheme, path=path)
    resolved = _resolve_ips(hostname, port)
    if not resolved:
        return OutboundTarget(False, f"callback_url host could not be resolved: {hostname}")
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


def _send(target: OutboundTarget, body: bytes, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    assert target.hostname and target.port and target.scheme
    conn: http.client.HTTPConnection
    if target.scheme == "https":
        if target.pinned_ip:
            conn = _PinnedHTTPSConnection(target.hostname, target.pinned_ip, target.port, timeout)
        else:
            # Host matched the allowlist by name (operator-trusted): resolve normally, same as
            # any stdlib HTTPS client.
            conn = http.client.HTTPSConnection(target.hostname, target.port, timeout=timeout)
    else:
        # ponytail: plain-http path only ever reaches a loopback host (enforced above), so no
        # rebinding surface worth pinning; connect by hostname like a normal HTTP client.
        conn = http.client.HTTPConnection(target.hostname, target.port, timeout=timeout)
    try:
        conn.request("POST", target.path, body=body, headers=headers)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
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

    def deliver_run_completion(self, run: dict[str, Any]) -> dict[str, Any] | None:
        """Call once, right after a run reaches succeeded/failed. Runs the full bounded-retry
        loop synchronously in the caller's thread (the run's own background completion thread,
        never the HTTP request thread) and returns the final delivery row — or None if this
        run's _meta.reply did not request webhook delivery."""
        if run.get("state") not in {"succeeded", "failed"}:
            return None
        reply = _reply_of(run)
        if not reply or reply.get("mode") != "webhook":
            return None
        callback_url = reply.get("callback_url")
        if not isinstance(callback_url, str) or not callback_url:
            return None
        delivery = self._create_delivery(run["id"], callback_url, reply.get("correlation_id"))
        while True:
            delivery = self._attempt(delivery, run)
            if delivery["status"] != "pending":
                return delivery
            time.sleep(min(_BACKOFF_BASE_SECONDS * (2 ** (delivery["attempts"] - 1)), _BACKOFF_CAP_SECONDS))

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

    def _attempt(self, delivery: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.secret_key:
            return self.db.update_delivery(
                delivery["id"],
                status="blocked",
                last_error="ATLAS_SECRET_KEY is not configured; refusing to send an unsigned delivery",
            ) or delivery
        target = resolve_outbound_target(delivery["url"], self.settings.allowlist)
        if not target.allowed:
            return self.db.update_delivery(delivery["id"], status="blocked", last_error=target.reason) or delivery
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
            status, _body = _send(target, body, headers, self.settings.timeout_seconds)
            if 200 <= status < 300:
                return self.db.update_delivery(
                    delivery["id"], status="delivered", attempts=attempts, last_error=None, delivered_at=now_iso()
                ) or delivery
            error = f"receiver returned HTTP {status}"
        except (OSError, http.client.HTTPException) as exc:
            error = f"{type(exc).__name__}: {exc}"
        next_status = "pending" if attempts < delivery["max_attempts"] else "failed"
        return self.db.update_delivery(delivery["id"], status=next_status, attempts=attempts, last_error=error) or delivery


def _reply_of(run: dict[str, Any]) -> dict[str, Any] | None:
    reply = ((run.get("input") or {}).get("_meta") or {}).get("reply")
    return reply if isinstance(reply, dict) else None
