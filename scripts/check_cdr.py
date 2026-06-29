from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fleet.cdr import CDR_FIELDS, CDR_SCHEMA, aggregate_cdr, cdr_csv, write_cdr


def _run_event(idx: int, units: int = 1, seconds: float = 1.0) -> dict:
    return {
        "kind": "workflow_run",
        "status": "succeeded",
        "units": units,
        "seconds": seconds,
        "created_at": f"2026-06-{idx:02d}T00:00:00Z",
    }


def _job_event(idx: int) -> dict:
    return {"kind": "job", "units": 1, "seconds": 0.5, "created_at": f"2026-06-{idx:02d}T01:00:00Z"}


def main() -> None:
    period_start, period_end = "2026-06-01T00:00:00Z", "2026-06-30T23:59:59Z"

    # Synthetic multi-instance usage. tenant-a: 5 successful workflow_run (units 1..5),
    # 2 jobs, plus a failed and a cancelled run that must NOT be billed. tenant-b: 3
    # successful runs. tenant-c: only a failed run (must produce no billable row).
    failed = dict(_run_event(9), status="failed", units=9)
    cancelled = dict(_run_event(10), status="cancelled", units=9)
    tenant_a = [_run_event(i, units=i) for i in range(1, 6)] + [_job_event(1), _job_event(2), failed, cancelled]
    tenant_b = [_run_event(i) for i in range(1, 4)]
    tenant_c = [dict(_run_event(1), status="failed")]
    events_by_tenant = {"tenant-a": tenant_a, "tenant-b": tenant_b, "tenant-c": tenant_c}

    cdr = aggregate_cdr(events_by_tenant, period_start, period_end)

    # One CDR per tenant.
    assert set(cdr) == {"tenant-a", "tenant-b", "tenant-c"}, set(cdr)
    # tenant-c had only a failed run -> nothing billable -> no rows.
    assert cdr["tenant-c"] == [], cdr["tenant-c"]

    # tenant-a: a workflow_run row whose count == billable workflow-runs (5) and a job row.
    a_rows = {row["event_type"]: row for row in cdr["tenant-a"]}
    assert set(a_rows) == {"workflow_run", "job"}, set(a_rows)
    # failed/cancelled runs excluded from the billable count and units
    assert a_rows["workflow_run"]["count"] == 5, a_rows["workflow_run"]
    assert a_rows["workflow_run"]["budget_units"] == 1 + 2 + 3 + 4 + 5, a_rows["workflow_run"]
    assert a_rows["workflow_run"]["seconds"] == 5.0
    assert a_rows["workflow_run"]["first_event_at"] == "2026-06-01T00:00:00Z"
    assert a_rows["workflow_run"]["last_event_at"] == "2026-06-05T00:00:00Z"
    assert a_rows["workflow_run"]["period_start"] == period_start
    assert a_rows["job"]["count"] == 2

    # tenant-b: 3 billable workflow-runs.
    b_rows = {row["event_type"]: row for row in cdr["tenant-b"]}
    assert b_rows["workflow_run"]["count"] == 3

    # Columns match the proposed schema exactly.
    csv_text = cdr_csv(cdr["tenant-a"])
    assert csv_text.splitlines()[0].startswith("# x-schema:") and CDR_SCHEMA in csv_text.splitlines()[0]
    header = csv_text.splitlines()[1]
    assert header == ",".join(CDR_FIELDS), header

    # Deterministic: re-export is byte-identical.
    assert cdr_csv(cdr["tenant-a"]) == csv_text

    with TemporaryDirectory() as tmp:
        first = write_cdr(Path(tmp) / "out1", cdr)
        second = write_cdr(Path(tmp) / "out2", cdr)
        assert set(first) == {"tenant-a", "tenant-b", "tenant-c"}  # one file per tenant
        for tenant in first:
            assert Path(first[tenant]).read_bytes() == Path(second[tenant]).read_bytes(), tenant

        # Tenants whose sanitized names collide still get distinct files (no overwrite).
        collide = aggregate_cdr({"acme/x": [_run_event(1)], "acme:x": [_run_event(2)]}, period_start, period_end)
        paths = write_cdr(Path(tmp) / "collide", collide)
        assert len(set(str(p) for p in paths.values())) == 2, paths

    # A CDR export must fail loudly if any instance pull fails (no silent partial bill).
    from fleet.fleet import Registry
    from fleet.cdr import pull_and_aggregate

    with TemporaryDirectory() as tmp:
        registry = Registry(Path(tmp) / "fleet.sqlite")
        registry.register({"tenant": "down", "base_url": "http://127.0.0.1:1", "status": "unknown"})
        try:
            pull_and_aggregate(registry)
        except RuntimeError as exc:
            assert "usage pull failed" in str(exc), str(exc)
        else:
            raise AssertionError("CDR export must fail when an instance pull fails")

    print("cdr check ok")


if __name__ == "__main__":
    main()
