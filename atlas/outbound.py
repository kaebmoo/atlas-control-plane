from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse


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
    return list(dict.fromkeys(info[4][0].split("%", 1)[0] for info in infos))


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
