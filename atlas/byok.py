"""BYOK (bring-your-own-key) key-injection helper.

Atlas core stores **no** model key. This helper implements the *option-b* path: it writes
a provider key into a target worker's own env/config file so thClaws can load it, and
audits the action (actor, target, provider, timestamp) WITHOUT ever logging, storing, or
returning the key value. The *option-a* forward interface (POST to a future thClaws
save-key endpoint) is defined below as a documented stub so it drops in later.

See docs/specs/byok-key-injection.md.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from .config import Config
from .db import Database, atomic_write_0600

# Default env var per provider; override with --env-var for anything else.
_PROVIDER_ENV_VARS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "azure": "AZURE_OPENAI_API_KEY",
}


def env_var_for(provider: str, override: str | None = None) -> str:
    if override:
        return override
    key = _PROVIDER_ENV_VARS.get(provider.lower())
    if not key:
        raise ValueError(f"no default env var for provider '{provider}'; pass an explicit env_var")
    return key


def _write_env_file(path: Path, env_var: str, value: str) -> None:
    """Upsert `env_var=value` in a KEY=VALUE env file. The new content is written through a
    0600 temp file and atomically replaced, so a short write or disk error can never leave
    the existing env file truncated/empty (it stays intact until the replace succeeds), and
    the secret is never written to a world-readable file."""
    path = Path(path)
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    replaced = False
    out: list[str] = []
    for line in lines:
        if line.startswith(f"{env_var}="):
            out.append(f"{env_var}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{env_var}={value}")
    atomic_write_0600(path, ("\n".join(out) + "\n").encode("utf-8"))


def inject_worker_key(
    db: Database,
    *,
    worker_ref: str,
    provider: str,
    key: str,
    config_path: str | Path,
    env_var: str | None = None,
    actor: str = "atlas-byok",
) -> dict[str, Any]:
    """option-b: write the model key into the target worker's env/config file and audit
    the action. The key is NEVER stored in Atlas, logged, or returned. Returns only
    non-secret metadata."""
    if not key:
        raise ValueError("key is required")
    if not worker_ref:
        raise ValueError("worker_ref is required")
    resolved_env_var = env_var_for(provider, env_var)
    # Audit (without the key) BEFORE writing, so a key is never installed without an audit
    # record — if the DB write fails the key is never written; if the file write fails the
    # attempt is still on record for an operator to investigate.
    db.audit(
        "byok.inject",
        "worker",
        worker_ref,
        {"provider": provider, "env_var": resolved_env_var, "config_path": str(config_path), "method": "option-b-env"},
        actor=actor,
    )
    _write_env_file(Path(config_path), resolved_env_var, key)
    return {
        "worker": worker_ref,
        "provider": provider,
        "env_var": resolved_env_var,
        "config_path": str(config_path),
        "method": "option-b-env",
    }


def forward_key_to_thclaws(worker_base_url: str, provider: str, key: str) -> dict[str, Any]:
    """option-a (NOT YET AVAILABLE): forward the key to a future thClaws save-key endpoint
    instead of writing a file. The intended contract, ready to implement when thClaws
    ships it:

        POST {worker_base_url}/agent/keys
        Authorization: Bearer <worker token>
        { "provider": "<provider>", "key": "<key>" }   -> 204, key held by thClaws only

    Atlas core still stores nothing; it only relays the key to the worker and audits the
    action. Implement here when the endpoint exists; until then option-b (env injection)
    is the supported path.
    """
    raise NotImplementedError(
        "thClaws save-key endpoint (option-a) is not available yet; use inject_worker_key (option-b)"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Inject a BYOK model key into a worker's env (option-b)")
    parser.add_argument("--worker", required=True, help="worker id/name for the audit trail")
    parser.add_argument("--provider", required=True, help="e.g. openai, anthropic")
    parser.add_argument("--config", required=True, type=Path, help="target worker env file to write")
    parser.add_argument("--env-var", default=None, help="override the env var name")
    parser.add_argument(
        "--key-env",
        default="ATLAS_BYOK_KEY",
        help="env var to read the key FROM (never pass the key as an argument; it would leak in ps/history)",
    )
    args = parser.parse_args(argv)

    key = os.getenv(args.key_env)
    if not key:
        parser.error(f"set the key in ${args.key_env} (it is read from the environment, never an argument)")

    config = Config.from_env()
    db = Database(config.db_path, secret_key=config.secret_key)
    result = inject_worker_key(
        db,
        worker_ref=args.worker,
        provider=args.provider,
        key=key,
        config_path=args.config,
        env_var=args.env_var,
        actor="atlas-byok-cli",
    )
    # Print only non-secret metadata.
    print(f"injected {result['provider']} key -> {result['env_var']} in {result['config_path']} (worker {result['worker']})", file=sys.stdout)


if __name__ == "__main__":
    main()
