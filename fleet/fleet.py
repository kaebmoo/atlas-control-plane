"""Atlas Fleet — a minimal instance registry + provisioning/health/usage-pull.

Fleet is a SEPARATE component from Atlas core. It owns its own small SQLite registry
and shares nothing with any tenant database; it only talks to Atlas instances over HTTP
(and, for local provisioning, seeds an instance's own DB before starting it). Atlas core
has no knowledge of Fleet and no tenant logic — the silo invariant stays intact.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from atlas.db import Database, new_id, now_iso  # noqa: E402

DEFAULT_REGISTRY = Path(os.getenv("ATLAS_FLEET_DB", ROOT / "data" / "fleet.sqlite"))
INSTANCE_DB_NAME = "atlas.sqlite"
_REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS instances (
  id TEXT PRIMARY KEY,
  tenant TEXT NOT NULL,
  base_url TEXT NOT NULL,
  region TEXT NOT NULL DEFAULT '',
  version TEXT NOT NULL DEFAULT '',
  admin_token_ref TEXT,
  status TEXT NOT NULL DEFAULT 'unknown',
  last_health_at TEXT,
  created_at TEXT NOT NULL
);
"""


class Registry:
    """SQLite registry of Atlas instances. Admin tokens are NOT stored here — only a
    reference (`admin_token_ref`); the raw token lives in a 0600 sidecar secrets file so
    it never lands in the registry, logs, or API responses."""

    def __init__(self, path: Path = DEFAULT_REGISTRY):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.secrets_path = self.path.parent / "fleet-secrets.json"
        with self._connect() as conn:
            conn.executescript(_REGISTRY_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def register(self, instance: dict[str, Any]) -> dict[str, Any]:
        instance_id = instance.get("id") or new_id("inst")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO instances(id, tenant, base_url, region, version,
                  admin_token_ref, status, last_health_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    instance_id,
                    instance["tenant"],
                    instance["base_url"],
                    instance.get("region") or "",
                    instance.get("version") or "",
                    instance.get("admin_token_ref"),
                    instance.get("status") or "unknown",
                    instance.get("last_health_at"),
                    now_iso(),
                ),
            )
            conn.commit()
        return self.get(instance_id) or {}

    def get(self, instance_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM instances WHERE id = ?", (instance_id,)).fetchone()
        return dict(row) if row else None

    def list(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM instances ORDER BY created_at, id").fetchall()
        return [dict(row) for row in rows]

    def update_health(self, instance_id: str, *, status: str, version: str | None, last_health_at: str) -> None:
        with self._connect() as conn:
            if version:
                conn.execute(
                    "UPDATE instances SET status = ?, version = ?, last_health_at = ? WHERE id = ?",
                    (status, version, last_health_at, instance_id),
                )
            else:
                conn.execute(
                    "UPDATE instances SET status = ?, last_health_at = ? WHERE id = ?",
                    (status, last_health_at, instance_id),
                )
            conn.commit()

    # --- token secrets sidecar (0600) ---
    def _load_secrets(self) -> dict[str, str]:
        if not self.secrets_path.exists():
            return {}
        return json.loads(self.secrets_path.read_text(encoding="utf-8"))

    def store_token(self, ref: str, token: str) -> None:
        data = self._load_secrets()
        data[ref] = token
        # Create/truncate at 0600 from the start (umask only clears bits, so 0o600
        # holds under any umask), so a freshly seeded admin token is never momentarily
        # world-readable. The trailing chmod tightens a pre-existing looser file.
        fd = os.open(self.secrets_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            # fchmod before writing: an existing file keeps its old mode through O_CREAT,
            # but O_TRUNC emptied it, so tightening now means tokens are only ever written
            # to a 0600 file (no world-readable window).
            os.fchmod(fd, 0o600)
            os.write(fd, json.dumps(data).encode("utf-8"))
        finally:
            os.close(fd)

    def token_for(self, ref: str | None) -> str | None:
        if not ref:
            return None
        return self._load_secrets().get(ref)


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _await_healthy(base_url: str, proc: subprocess.Popen, timeout: float = 15.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"atlas instance exited early (code {proc.returncode})")
        try:
            with urllib.request.urlopen(base_url + "/healthz", timeout=1) as resp:
                if resp.status == 200:
                    return json.loads(resp.read()).get("version", "")
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.1)
    proc.terminate()
    raise RuntimeError(f"atlas instance did not become healthy: {base_url}")


def provision_local(
    registry: Registry,
    tenant: str,
    *,
    region: str = "local",
    host: str = "127.0.0.1",
    port: int | None = None,
    data_dir: str | Path | None = None,
    python: str | None = None,
) -> tuple[dict[str, Any], subprocess.Popen]:
    """Provision an Atlas instance as a local subprocess: seed an admin token into its
    own DB, run migrations (on startup), start it, wait for /healthz, then register it.
    Returns (instance_record, process); the caller owns the process lifecycle."""
    port = port or _free_port()
    data_dir = Path(data_dir) if data_dir else Path(tempfile.mkdtemp(prefix="atlas-inst-"))
    data_dir.mkdir(parents=True, exist_ok=True)
    instance_db = data_dir / INSTANCE_DB_NAME

    # Seed admin + token directly (create-admin is interactive; remote/compose targets
    # run `python3 -m atlas.admin create-admin` from cloud-init instead). init() here
    # also runs the migrations the instance will use.
    db = Database(instance_db)
    with db.as_actor("atlas-fleet"):
        user = db.create_user(f"admin-{tenant}", secrets.token_urlsafe(16), role="admin")
        _, raw_token = db.create_api_token(user["id"], "fleet-bootstrap")
    token_ref = new_id("atok")
    registry.store_token(token_ref, raw_token)

    env = {**os.environ, "ATLAS_DB": str(instance_db), "ATLAS_HOME": str(data_dir)}
    env.pop("ATLAS_LOOPBACK_NO_AUTH", None)  # auth required; we hold a real token
    proc = subprocess.Popen(
        [python or sys.executable, "-m", "atlas", "--host", host, "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    base_url = f"http://{host}:{port}"
    try:
        version = _await_healthy(base_url, proc)
    except Exception:
        proc.terminate()
        raise

    instance = registry.register(
        {
            "tenant": tenant,
            "base_url": base_url,
            "region": region,
            "version": version,
            "admin_token_ref": token_ref,
            "status": "online",
            "last_health_at": now_iso(),
        }
    )
    return instance, proc


def check_health(registry: Registry, instance: dict[str, Any]) -> dict[str, Any]:
    status, version = "offline", None
    try:
        with urllib.request.urlopen(instance["base_url"] + "/healthz", timeout=3) as resp:
            if resp.status == 200:
                status = "online"
                version = json.loads(resp.read()).get("version") or None
    except (urllib.error.URLError, ConnectionError, OSError):
        status = "offline"
    registry.update_health(instance["id"], status=status, version=version, last_health_at=now_iso())
    return registry.get(instance["id"]) or {}


def poll_all(registry: Registry) -> list[dict[str, Any]]:
    return [check_health(registry, instance) for instance in registry.list()]


def pull_usage(
    registry: Registry,
    from_at: str | None = None,
    to_at: str | None = None,
    instance_id: str | None = None,
    strict: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Pull raw usage_events from each instance's GET /api/usage (authenticated with the
    instance's seeded token). Returns {instance_id: [events]}. With strict=True a failed
    instance pull raises (used by CDR export so a billing artifact is never partial);
    otherwise a failed instance yields an empty list and a stderr note (best-effort dump)."""
    targets = [registry.get(instance_id)] if instance_id else registry.list()
    result: dict[str, list[dict[str, Any]]] = {}
    for instance in targets:
        if not instance:
            continue
        params = {key: value for key, value in (("from", from_at), ("to", to_at)) if value}
        url = instance["base_url"] + "/api/usage" + (("?" + urlencode(params)) if params else "")
        token = registry.token_for(instance.get("admin_token_ref"))
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=10) as resp:
                payload = json.loads(resp.read())
            result[instance["id"]] = payload.get("usage", [])
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            if strict:
                raise RuntimeError(f"usage pull failed for instance {instance['id']} ({instance['base_url']}): {exc}") from exc
            result[instance["id"]] = []
            print(f"usage-pull failed for {instance['id']}: {exc}", file=sys.stderr)
    return result


def compose_stub(tenant: str, port: int = 8787) -> str:
    """A docker-compose IaC stub (not a bespoke orchestrator). The operator runs
    `docker compose up -d`, then seeds admin + registers. systemd is the alt target
    (docs/ops/atlas.service); GDCC/k8s are noted alternates in the GA plan."""
    return (
        "# Atlas instance for tenant '" + tenant + "' — IaC stub, fill image/volumes.\n"
        "services:\n"
        f"  atlas-{tenant}:\n"
        "    image: atlas:local            # or build: ../\n"
        "    command: python3 -m atlas --host 0.0.0.0 --port 8787\n"
        "    environment:\n"
        "      ATLAS_DB: /data/atlas.sqlite\n"
        "      ATLAS_SECRET_KEY: ${ATLAS_SECRET_KEY:?set me}\n"
        "    volumes:\n"
        f"      - ./atlas-{tenant}-data:/data\n"
        "    ports:\n"
        f"      - \"{port}:8787\"\n"
        "    restart: unless-stopped\n"
    )


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=True, indent=2, default=str))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="atlas-fleet", description="Atlas Fleet — instance registry and operations")
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY), help="path to the fleet registry SQLite file")
    sub = parser.add_subparsers(dest="command", required=True)

    p_prov = sub.add_parser("provision", help="provision an Atlas instance and register it")
    p_prov.add_argument("--tenant", required=True)
    p_prov.add_argument("--target", choices=["local", "compose"], default="local")
    p_prov.add_argument("--region", default="local")
    p_prov.add_argument("--host", default="127.0.0.1")
    p_prov.add_argument("--port", type=int, default=None)
    p_prov.add_argument("--data-dir", default=None)

    sub.add_parser("list", help="list registered instances")
    sub.add_parser("health", help="poll /healthz for every instance and update status")

    p_usage = sub.add_parser("usage-pull", help="pull raw usage events from instances")
    p_usage.add_argument("--from", dest="from_at", default=None)
    p_usage.add_argument("--to", dest="to_at", default=None)
    p_usage.add_argument("--instance", dest="instance_id", default=None)

    p_cdr = sub.add_parser("cdr", help="export per-tenant CDR CSVs (proposed schema)")
    p_cdr.add_argument("--from", dest="from_at", default=None)
    p_cdr.add_argument("--to", dest="to_at", default=None)
    p_cdr.add_argument("--out-dir", dest="out_dir", required=True)

    args = parser.parse_args(argv)
    registry = Registry(Path(args.registry))

    if args.command == "provision":
        if args.target == "compose":
            print(compose_stub(args.tenant, args.port or 8787))
            return
        instance, proc = provision_local(
            registry, args.tenant, region=args.region, host=args.host, port=args.port, data_dir=args.data_dir
        )
        _print({"provisioned": instance, "pid": proc.pid})
        return
    if args.command == "list":
        _print(registry.list())
        return
    if args.command == "health":
        _print(poll_all(registry))
        return
    if args.command == "usage-pull":
        _print(pull_usage(registry, args.from_at, args.to_at, args.instance_id))
        return
    if args.command == "cdr":
        from .cdr import pull_and_aggregate, write_cdr

        written = write_cdr(args.out_dir, pull_and_aggregate(registry, args.from_at, args.to_at))
        _print({tenant: str(path) for tenant, path in written.items()})
        return


if __name__ == "__main__":
    main()
