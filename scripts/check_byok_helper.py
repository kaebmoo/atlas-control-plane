from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.byok import forward_key_to_thclaws, inject_worker_key
from atlas.db import Database

SECRET = "sk-super-secret-byok-value-9f83a1c2"


def main() -> None:
    with TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "atlas.sqlite")
        config_path = Path(tmp) / "worker-x.env"

        result = inject_worker_key(
            db,
            worker_ref="worker-x",
            provider="openai",
            key=SECRET,
            config_path=config_path,
        )

        # 1. The key is written to the target worker's env file (not Atlas).
        contents = config_path.read_text(encoding="utf-8")
        assert f"OPENAI_API_KEY={SECRET}" in contents, contents
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600, oct(config_path.stat().st_mode)

        # 2. The action is audited (actor, target, provider) WITHOUT the key.
        audit = db.list_audit()
        injection = next(entry for entry in audit if entry["action"] == "byok.inject")
        assert injection["resource_id"] == "worker-x"
        assert injection["details"]["provider"] == "openai"
        assert injection["details"]["env_var"] == "OPENAI_API_KEY"
        assert SECRET not in json.dumps(injection), "key leaked into the audit entry"

        # 3. The key never appears in the return value, the audit API, or the Atlas DB.
        assert SECRET not in json.dumps(result), "key leaked into the return value"
        assert SECRET not in json.dumps(db.list_audit()), "key leaked into the audit listing (API surface)"
        db_bytes = (Path(tmp) / "atlas.sqlite").read_bytes()
        assert SECRET.encode("utf-8") not in db_bytes, "key leaked into the Atlas database file"

        # 4. Re-injection updates the same env file, stays 0600, no duplicate var.
        inject_worker_key(db, worker_ref="worker-x", provider="openai", key="sk-rotated-value", config_path=config_path)
        updated = config_path.read_text(encoding="utf-8")
        assert updated.count("OPENAI_API_KEY=") == 1, updated
        assert "OPENAI_API_KEY=sk-rotated-value" in updated
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600

        # 5. option-a (forward to thClaws) is a documented stub, not silently a no-op.
        try:
            forward_key_to_thclaws("http://127.0.0.1:1", "openai", SECRET)
        except NotImplementedError:
            pass
        else:
            raise AssertionError("forward_key_to_thclaws must raise until thClaws ships the endpoint")

        # 6. Injecting into an EXISTING looser-mode file tightens it to 0600 (no
        #    world-readable window) and preserves unrelated lines.
        loose = Path(tmp) / "loose.env"
        loose.write_text("OTHER=keep\n", encoding="utf-8")
        os.chmod(loose, 0o644)
        inject_worker_key(db, worker_ref="worker-y", provider="anthropic", key="sk-y-secret", config_path=loose)
        assert stat.S_IMODE(loose.stat().st_mode) == 0o600, oct(loose.stat().st_mode)
        body = loose.read_text(encoding="utf-8")
        assert "OTHER=keep" in body and "ANTHROPIC_API_KEY=sk-y-secret" in body, body

    print("byok helper check ok")


if __name__ == "__main__":
    main()
