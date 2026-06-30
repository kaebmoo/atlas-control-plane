from __future__ import annotations

import json
import stat
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas import __version__
from atlas.db import Database
from fleet.fleet import INSTANCE_DB_NAME, Registry, _await_healthy, check_health, poll_all, provision_local, pull_usage


class _DummyProc:
    """Stand-in for a live atlas subprocess: poll() None means 'still running'."""

    returncode = 0

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        return None


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

        # a failed provision (instance never becomes healthy) must not orphan a secret:
        # the admin token is persisted only after /healthz is green. `false` stands in for
        # an atlas process that exits immediately.
        reg2 = Registry(Path(tmp) / "fleet2" / "fleet2.sqlite")  # own dir = own secrets sidecar
        try:
            provision_local(reg2, "tenant-x", data_dir=Path(tmp) / "inst-x", python="false")
        except RuntimeError:
            pass
        else:
            raise AssertionError("provision must fail when the instance never starts")
        assert reg2._load_secrets() == {}, "failed provision must not store an admin token"

        # a reachable-but-unhealthy instance (HTTP 200 carrying {"ok": false}) must be
        # recorded offline, not online — check_health honors the ok flag.
        class _UnhealthyHandler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                return

            def do_GET(self) -> None:
                body = json.dumps({"ok": False, "version": "x"}).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        mock = ThreadingHTTPServer(("127.0.0.1", 0), _UnhealthyHandler)
        threading.Thread(target=mock.serve_forever, daemon=True).start()
        try:
            host, port = mock.server_address
            record = registry.register({"tenant": "t-unhealthy", "base_url": f"http://{host}:{port}"})
            assert check_health(registry, record)["status"] == "offline", "ok:false must be offline"
            # _await_healthy must ALSO honor the ok flag (not just check_health): a 200 carrying
            # {"ok": false} is not yet healthy, so provisioning keeps waiting and times out.
            try:
                _await_healthy(f"http://{host}:{port}", _DummyProc(), timeout=0.5)
            except RuntimeError:
                pass
            else:
                raise AssertionError("_await_healthy must not accept a 200 with ok:false")
        finally:
            mock.shutdown()
            mock.server_close()

        # a healthy instance (200 with {"ok": true}) reports its version through _await_healthy
        class _HealthyHandler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                return

            def do_GET(self) -> None:
                body = json.dumps({"ok": True, "version": "9.9"}).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        healthy_mock = ThreadingHTTPServer(("127.0.0.1", 0), _HealthyHandler)
        threading.Thread(target=healthy_mock.serve_forever, daemon=True).start()
        try:
            host, port = healthy_mock.server_address
            assert _await_healthy(f"http://{host}:{port}", _DummyProc(), timeout=2) == "9.9"
        finally:
            healthy_mock.shutdown()
            healthy_mock.server_close()

        # concurrent token writes must not lose entries even across SEPARATE Registry objects
        # (stand-ins for separate processes) at the same path — the per-object lock can't span
        # them, so the cross-process flock is what serializes the read-modify-write.
        reg_path = Path(tmp) / "fleet3" / "fleet3.sqlite"
        Registry(reg_path)  # initialize the sidecar dir once
        errors: list[Exception] = []

        def store(i: int) -> None:
            try:
                Registry(reg_path).store_token(f"ref-{i}", f"tok-{i}")  # fresh object per thread
            except Exception as exc:  # noqa: BLE001 - recorded, asserted below
                errors.append(exc)

        workers = [threading.Thread(target=store, args=(i,)) for i in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        assert not errors, f"concurrent store_token raised: {errors[:2]}"
        verify = Registry(reg_path)
        for i in range(8):
            assert verify.token_for(f"ref-{i}") == f"tok-{i}", f"lost token ref-{i}"

        # A provision that fails at register() (after store_token) must roll back the secret —
        # no orphan ref — and a retry on the same data_dir must succeed (idempotent seeding).
        reg_rb = Registry(Path(tmp) / "fleet-rb" / "fleet.sqlite")
        data_dir = Path(tmp) / "inst-rb"
        original_register = reg_rb.register

        def _boom(*_args: object, **_kwargs: object) -> dict:
            raise RuntimeError("register boom")

        reg_rb.register = _boom  # type: ignore[method-assign]
        try:
            provision_local(reg_rb, "tenant-rb", data_dir=data_dir)
        except RuntimeError:
            pass
        else:
            raise AssertionError("provision must propagate a register() failure")
        assert reg_rb._load_secrets() == {}, "failed provision must roll back the stored secret"

        # A register() that COMMITS the row and THEN raises must leave NO orphan row (the
        # pre-generated id lets rollback deregister it) and no orphan secret.
        def _commit_then_raise(payload: dict) -> dict:
            original_register(payload)
            raise RuntimeError("post-commit boom")

        reg_rb.register = _commit_then_raise  # type: ignore[method-assign]
        try:
            provision_local(reg_rb, "tenant-pc", data_dir=Path(tmp) / "inst-pc")
        except RuntimeError:
            pass
        else:
            raise AssertionError("provision must propagate a post-commit register failure")
        assert reg_rb.list() == [], "post-commit failure must roll back the registry row (no orphan instance)"
        assert reg_rb._load_secrets() == {}, "post-commit failure must roll back the secret"

        reg_rb.register = original_register  # type: ignore[method-assign]
        instance_rb, proc_rb = provision_local(reg_rb, "tenant-rb", data_dir=data_dir)
        try:
            assert instance_rb["status"] == "online", "idempotent retry on the same data_dir must succeed"
            assert len(reg_rb._load_secrets()) == 1, "a successful retry stores exactly one secret"
        finally:
            proc_rb.terminate()
            proc_rb.wait(timeout=5)

        # Idempotent seeding must REFUSE to reuse a non-admin / disabled seeded user (it would
        # mint a Fleet token that then 401/403s on /api/usage).
        na_dir = Path(tmp) / "inst-na"
        na_db = Database(na_dir / INSTANCE_DB_NAME)
        with na_db.as_actor("test"):
            na_db.create_user("admin-tenant-na", "pw", role="viewer")
        try:
            provision_local(Registry(Path(tmp) / "fleet-na" / "f.sqlite"), "tenant-na", data_dir=na_dir)
        except ValueError as exc:
            assert "not an active admin" in str(exc), exc
        else:
            raise AssertionError("provision must refuse to reuse a non-admin seeded user")

    print("fleet check ok")


if __name__ == "__main__":
    main()
