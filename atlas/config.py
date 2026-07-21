from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _csv_env(name: str) -> tuple[str, ...]:
    raw = os.getenv(name) or ""
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    db_path: Path
    api_token: str | None
    request_timeout_seconds: float
    enable_loopback_without_token: bool
    secret_key: str | None = None
    upload_dir: Path | None = None
    max_upload_bytes: int = 10 * 1024 * 1024
    request_log: bool = False
    require_signed_packs: bool = False
    outbound_allowlist: tuple[str, ...] = ()
    outbound_max_attempts: int = 5
    outbound_timeout_seconds: float = 10.0
    # T3 async execution: the externally reachable Atlas base URL workers deliver callbacks to.
    # Unset -> execution:"callback" is rejected at submit validation time.
    public_base_url: str | None = None
    callback_timeout_seconds: float = 3600.0
    serve_ui: bool = True
    cors_origins: tuple[str, ...] = ()
    session_token_ttl_seconds: int = 8 * 60 * 60
    max_active_sessions: int = 5
    login_rate_limit_attempts: int = 5
    login_rate_limit_window_seconds: int = 60
    login_rate_limit_cooldown_seconds: int = 60

    @classmethod
    def from_env(cls) -> "Config":
        root = Path(os.getenv("ATLAS_HOME", Path.cwd())).resolve()
        db_path = Path(os.getenv("ATLAS_DB", root / "data" / "atlas.sqlite")).resolve()
        upload_dir = Path(os.getenv("ATLAS_UPLOAD_DIR", db_path.parent / "uploads")).resolve()
        return cls(
            host=os.getenv("ATLAS_HOST", "127.0.0.1"),
            port=int(os.getenv("ATLAS_PORT", "8787")),
            db_path=db_path,
            api_token=os.getenv("ATLAS_API_TOKEN") or None,
            request_timeout_seconds=float(os.getenv("ATLAS_REQUEST_TIMEOUT", "30")),
            enable_loopback_without_token=_bool_env("ATLAS_LOOPBACK_NO_AUTH", False),
            secret_key=os.getenv("ATLAS_SECRET_KEY") or None,
            upload_dir=upload_dir,
            max_upload_bytes=int(os.getenv("ATLAS_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024))),
            request_log=_bool_env("ATLAS_REQUEST_LOG", False),
            require_signed_packs=_bool_env("ATLAS_REQUIRE_SIGNED_PACKS", False),
            outbound_allowlist=_csv_env("ATLAS_OUTBOUND_ALLOWLIST"),
            outbound_max_attempts=int(os.getenv("ATLAS_OUTBOUND_MAX_ATTEMPTS", "5")),
            outbound_timeout_seconds=float(os.getenv("ATLAS_OUTBOUND_TIMEOUT", "10")),
            public_base_url=(os.getenv("ATLAS_PUBLIC_BASE_URL") or "").rstrip("/") or None,
            callback_timeout_seconds=float(os.getenv("ATLAS_CALLBACK_TIMEOUT_SECONDS", "3600")),
            serve_ui=_bool_env("ATLAS_SERVE_UI", True),
            cors_origins=_csv_env("ATLAS_CORS_ORIGINS"),
            session_token_ttl_seconds=_positive_int_env("ATLAS_SESSION_TOKEN_TTL_SECONDS", 8 * 60 * 60),
            max_active_sessions=_positive_int_env("ATLAS_MAX_ACTIVE_SESSIONS", 5),
            login_rate_limit_attempts=_positive_int_env("ATLAS_LOGIN_RATE_LIMIT_ATTEMPTS", 5),
            login_rate_limit_window_seconds=_positive_int_env("ATLAS_LOGIN_RATE_LIMIT_WINDOW_SECONDS", 60),
            login_rate_limit_cooldown_seconds=_positive_int_env("ATLAS_LOGIN_RATE_LIMIT_COOLDOWN_SECONDS", 60),
        )

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"
