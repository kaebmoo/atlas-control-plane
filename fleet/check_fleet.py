from __future__ import annotations

import stat
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas import __version__
from atlas.db import Database
from fleet.fleet import INSTANCE_DB_NAME, Registry, check_health, poll_all, provision_local, pull_usage


def main() -> None:
    with TemporaryDirectory() as tmp:
        registry = Registry(Path(tmp) / "fleet.sqlite")
        instance_dir = Path(tmp) / "inst-a"

        # provision -> register
        instance, proc = provision_local(registry, "tenant-a", data_dir=instance_dir)
        try:
            assert instance["tenant"] == "tenant-a"
            assert instance["status"] == "online"
            assert instance["version"] == __version__, instance["version"]
            assert instance["admin_token_ref"], "instance must carry an admin_token_ref"
            assert registry.get(instance["id"])["base_url"] == instance["base_url"]
            assert len(registry.list()) == 1

            # the raw token is never stored in the registry row (no plaintext)
            raw_token = registry.token_for(instance["admin_token_ref"])
            assert raw_token and raw_token not in str(registry.get(instance["id"]))
            # secrets sidecar is 0600
            mode = stat.S_IMODE(registry.secrets_path.stat().st_mode)
            assert mode == 0o600, oct(mode)

            # health green
            healthy = check_health(registry, instance)
            assert healthy["status"] == "online" and healthy["last_health_at"]

            # seed a usage event on the instance, then usage-pull returns it
            inst_db = Database(instance_dir / INSTANCE_DB_NAME)
            inst_db.emit_usage_event(
                {"idempotency_key": "evt-1", "kind": "workflow_run", "units": 1, "status": "succeeded"}
            )
            pulled = pull_usage(registry)
            events = pulled[instance["id"]]
            assert len(events) == 1, events
            assert events[0]["kind"] == "workflow_run"
        finally:
            proc.terminate()
            proc.wait(timeout=5)

        # once the instance is down, health flips to offline
        offline = poll_all(registry)[0]
        assert offline["status"] == "offline", offline

    print("fleet check ok")


if __name__ == "__main__":
    main()
