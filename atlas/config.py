from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    db_path: Path
    api_token: str | None
    request_timeout_seconds: float
    enable_loopback_without_token: bool

    @classmethod
    def from_env(cls) -> "Config":
        root = Path(os.getenv("ATLAS_HOME", Path.cwd())).resolve()
        db_path = Path(os.getenv("ATLAS_DB", root / "data" / "atlas.sqlite")).resolve()
        return cls(
            host=os.getenv("ATLAS_HOST", "127.0.0.1"),
            port=int(os.getenv("ATLAS_PORT", "8787")),
            db_path=db_path,
            api_token=os.getenv("ATLAS_API_TOKEN") or None,
            request_timeout_seconds=float(os.getenv("ATLAS_REQUEST_TIMEOUT", "30")),
            enable_loopback_without_token=_bool_env("ATLAS_LOOPBACK_NO_AUTH", True),
        )

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"
