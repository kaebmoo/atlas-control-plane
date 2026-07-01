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
        )

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"
