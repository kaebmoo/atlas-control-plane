"""Concurrency stress (the CONCURRENCY axis — distinct from input robustness in check_fuzz):
hammer the atomic state transitions from many threads and assert the invariants the per-round
race bugs violated (state converges to terminal, never tears or wedges). The ASSERTIONS are
invariants, not timing checks — but thread scheduling varies, so a green run is strong EVIDENCE,
not a proof of absence. It complements (does not replace) the per-method unit checks."""

from __future__ import annotations

import random
import sys
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atlas.db import Database

random.seed(1234)  # reproducible interleavings-ish (thread scheduling still varies)


def stress_finalize(db: Database, runs: int = 100) -> None:
    """Concurrent runner-finish (succeeded, allowed_from=('running',)) vs cancel (allowed_from
    =None) on each running run: every run must end terminal — exactly one transition wins, no
    run is left 'running' (torn) or in a non-terminal limbo."""
    ids = [db.create_workflow_run({"name": f"r{i}", "state": "running"})["id"] for i in range(runs)]
    threads: list[threading.Thread] = []
    for rid in ids:
        threads.append(threading.Thread(target=lambda r=rid: db.finalize_workflow_run(r, "succeeded", allowed_from=("running",))))
        threads.append(threading.Thread(target=lambda r=rid: db.finalize_workflow_run(r, "cancelled")))
    random.shuffle(threads)
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for rid in ids:
        state = db.get_workflow_run(rid)["state"]
        assert state in ("succeeded", "cancelled"), f"run left non-terminal under finalize/cancel race: {state}"


def stress_job_start_cancel(db: Database, jobs: int = 100) -> None:
    """Concurrent try_start_job vs mark_cancel_requested on each queued job: every job must end
    'running' or 'cancel_requested' (never stuck 'queued', never a torn state), and try_start
    must report success at most once per job."""
    worker = db.upsert_worker({"base_url": "http://127.0.0.1:1", "name": "w"})
    ids = [db.create_job({"worker_id": worker["id"], "prompt": "x", "state": "queued"})["id"] for _ in range(jobs)]
    starts: dict[str, int] = {jid: 0 for jid in ids}
    lock = threading.Lock()

    def start(jid: str) -> None:
        if db.try_start_job(jid):
            with lock:
                starts[jid] += 1

    threads: list[threading.Thread] = []
    for jid in ids:
        threads.append(threading.Thread(target=start, args=(jid,)))
        threads.append(threading.Thread(target=lambda j=jid: db.mark_cancel_requested(j)))
    random.shuffle(threads)
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for jid in ids:
        state = db.get_job(jid)["state"]
        assert state in ("running", "cancel_requested"), f"job in unexpected state under start/cancel race: {state}"
        assert starts[jid] <= 1, f"job started more than once: {jid}"


def main() -> None:
    with TemporaryDirectory() as tmp:
        stress_finalize(Database(Path(tmp) / "finalize.sqlite"))
        stress_job_start_cancel(Database(Path(tmp) / "jobs.sqlite"))
    print("stress check ok")


if __name__ == "__main__":
    main()
