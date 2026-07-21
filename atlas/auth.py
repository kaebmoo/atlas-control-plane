from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass, field


PASSWORD_ITERATIONS = 600_000
PASSWORD_SCHEME = "pbkdf2_sha256"


@dataclass(frozen=True)
class LoginRateLimitDecision:
    retry_after_seconds: int | None = None
    newly_limited: bool = False

    @property
    def allowed(self) -> bool:
        return self.retry_after_seconds is None


@dataclass
class _LoginAttempts:
    failed_at: list[float] = field(default_factory=list)
    blocked_until: float = 0.0
    last_seen: float = 0.0


class LoginRateLimiter:
    """Small, bounded pre-PBKDF2 limiter keyed by normalized username + peer IP.

    State deliberately lives in process memory: a process restart clears the short
    defensive window rather than adding a durable attacker-controlled write path.
    Production deployments should also rate-limit at their reverse proxy.
    """

    def __init__(
        self, max_attempts: int, window_seconds: float, cooldown_seconds: float, *, max_entries: int = 4096
    ) -> None:
        if min(max_attempts, window_seconds, cooldown_seconds, max_entries) <= 0:
            raise ValueError("login rate-limit settings must be positive")
        self.max_attempts = int(max_attempts)
        self.window_seconds = float(window_seconds)
        self.cooldown_seconds = float(cooldown_seconds)
        self.max_entries = int(max_entries)
        self._entries: dict[tuple[str, str], _LoginAttempts] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(username: str, client_ip: str) -> tuple[str, str]:
        return (str(username).strip().casefold(), str(client_ip))

    def check(self, username: str, client_ip: str) -> LoginRateLimitDecision:
        now = time.monotonic()
        with self._lock:
            self._cleanup(now)
            entry = self._entries.get(self._key(username, client_ip))
            if entry is None or entry.blocked_until <= now:
                return LoginRateLimitDecision()
            entry.last_seen = now
            return LoginRateLimitDecision(max(1, int(entry.blocked_until - now + 0.999)))

    def record_failure(self, username: str, client_ip: str) -> LoginRateLimitDecision:
        now = time.monotonic()
        key = self._key(username, client_ip)
        with self._lock:
            self._cleanup(now)
            entry = self._entries.setdefault(key, _LoginAttempts())
            entry.failed_at = [at for at in entry.failed_at if at > now - self.window_seconds]
            entry.failed_at.append(now)
            entry.last_seen = now
            if len(entry.failed_at) < self.max_attempts:
                return LoginRateLimitDecision()
            entry.blocked_until = now + self.cooldown_seconds
            entry.failed_at.clear()
            return LoginRateLimitDecision(max(1, int(self.cooldown_seconds + 0.999)), newly_limited=True)

    def record_success(self, username: str, client_ip: str) -> None:
        with self._lock:
            self._entries.pop(self._key(username, client_ip), None)

    def _cleanup(self, now: float) -> None:
        stale_before = now - max(self.window_seconds, self.cooldown_seconds)
        for key, entry in list(self._entries.items()):
            if entry.blocked_until <= now:
                entry.failed_at = [at for at in entry.failed_at if at > now - self.window_seconds]
            if not entry.failed_at and entry.blocked_until <= now and entry.last_seen < stale_before:
                del self._entries[key]
        if len(self._entries) > self.max_entries:
            for key, _entry in sorted(self._entries.items(), key=lambda item: item[1].last_seen)[: len(self._entries) - self.max_entries]:
                del self._entries[key]


def hash_password(password: str) -> str:
    if not isinstance(password, str) or not password:
        raise ValueError("password is required")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return "$".join(
        (
            PASSWORD_SCHEME,
            str(PASSWORD_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, raw_iterations, raw_salt, raw_digest = encoded.split("$", 3)
        if scheme != PASSWORD_SCHEME:
            return False
        iterations = int(raw_iterations)
        salt = base64.urlsafe_b64decode(raw_salt.encode("ascii"))
        expected = base64.urlsafe_b64decode(raw_digest.encode("ascii"))
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def generate_api_token() -> str:
    return secrets.token_urlsafe(32)


def hash_api_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
