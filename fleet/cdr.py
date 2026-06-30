"""Fleet-side CDR (Call/Charge Detail Record) export.

Aggregates raw usage_events pulled from Atlas instances into a per-tenant, per-period
CDR CSV for NT's billing/mediation team. This is **export only** — no rating engine, no
invoices, no tenant_invoices, no ERP integration. The record schema is PROPOSED and
pending NT billing confirmation (see docs/specs/cdr-schema.md).
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from .fleet import Registry, pull_usage

# Marker noting the schema is proposed (the CSV carries it as x-schema, per the GA
# plan's external-decision register). Bump only when NT confirms the real schema.
CDR_SCHEMA = "atlas.cdr.v1-proposed"
CDR_FIELDS = (
    "tenant",
    "period_start",
    "period_end",
    "event_type",
    "count",
    "first_event_at",
    "last_event_at",
    "budget_units",
    "seconds",
)


def _billable(event_type: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The billable subset for an event type. The billing model bills only SUCCESSFUL
    workflow runs, so failed/cancelled runs are excluded; other types pass through."""
    if event_type == "workflow_run":
        return [event for event in events if event.get("status") == "succeeded"]
    return events


def aggregate_cdr(
    events_by_tenant: dict[str, list[dict[str, Any]]],
    period_start: str | None,
    period_end: str | None,
) -> dict[str, list[dict[str, Any]]]:
    """Group each tenant's usage events by event_type into CDR rows. Pure and
    deterministic: tenants and rows are sorted, timestamps come straight from the data.
    Counts reflect billable events only (see _billable)."""
    result: dict[str, list[dict[str, Any]]] = {}
    for tenant in sorted(events_by_tenant):
        by_kind: dict[str, list[dict[str, Any]]] = {}
        for event in events_by_tenant[tenant]:
            by_kind.setdefault(str(event.get("kind") or ""), []).append(event)
        rows: list[dict[str, Any]] = []
        for event_type in sorted(by_kind):
            group = _billable(event_type, by_kind[event_type])
            if not group:
                continue  # nothing billable of this type (e.g. only failed runs)
            timestamps = sorted(str(event.get("created_at")) for event in group if event.get("created_at"))
            rows.append(
                {
                    "tenant": tenant,
                    "period_start": period_start or "",
                    "period_end": period_end or "",
                    "event_type": event_type,
                    "count": len(group),
                    "first_event_at": timestamps[0] if timestamps else "",
                    "last_event_at": timestamps[-1] if timestamps else "",
                    "budget_units": sum(int(event.get("units") or 0) for event in group),
                    "seconds": round(sum(float(event.get("seconds") or 0) for event in group), 6),
                }
            )
        result[tenant] = rows
    return result


def _csv_safe(value: Any) -> Any:
    """Neutralize spreadsheet formula injection (e.g. a tenant named `=1+1`): a cell starting
    with = + - @ or a control char is prefixed with a single quote so it imports as text."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r", "\n"):
        return "'" + value
    return value


def cdr_csv(rows: list[dict[str, Any]]) -> str:
    """Serialize CDR rows to a deterministic CSV (stable field order, no generated-at
    timestamp), prefixed with the proposed-schema marker."""
    output = io.StringIO(newline="")
    output.write(f"# x-schema: {CDR_SCHEMA} (PROPOSED - pending NT billing confirmation)\n")
    writer = csv.DictWriter(output, fieldnames=CDR_FIELDS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: _csv_safe(row.get(field, "")) for field in CDR_FIELDS})
    return output.getvalue()


def _safe_name(tenant: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", tenant) or "tenant"


def _period_tag(period_start: str | None, period_end: str | None) -> str:
    """Filename-safe tag for the export period so different periods never collide in the same
    output directory. Unbounded ends fall back to 'begin'/'end'."""
    start = _safe_name(str(period_start)) if period_start else "begin"
    end = _safe_name(str(period_end)) if period_end else "end"
    return f"{start}_{end}"


def write_cdr(
    out_dir: Any,
    cdr_by_tenant: dict[str, list[dict[str, Any]]],
    period_start: str | None = None,
    period_end: str | None = None,
) -> dict[str, Any]:
    """Write one CDR CSV file per tenant under out_dir. The export period is encoded in the
    filename, so exporting a different period into the same directory never overwrites an
    earlier billing artifact. Deterministic: same input -> same filenames and same bytes.
    Returns {tenant: path}."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    period = _period_tag(period_start, period_end)
    # Disambiguate tenants whose sanitized names collide (e.g. "a/b" vs "a:b") so no tenant's
    # file is silently overwritten — one file per tenant per period is the contract.
    safe = {tenant: _safe_name(tenant) for tenant in cdr_by_tenant}
    collisions = {name for name, n in Counter(safe.values()).items() if n > 1}
    written: dict[str, Any] = {}
    for tenant in sorted(cdr_by_tenant):
        name = safe[tenant]
        if name in collisions:
            name = f"{name}-{hashlib.sha256(tenant.encode('utf-8')).hexdigest()[:8]}"
        path = out_dir / f"cdr-{name}-{period}.csv"
        _atomic_write_text(path, cdr_csv(cdr_by_tenant[tenant]))
        written[tenant] = path
    return written


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text via a temp file + os.replace so a crash (or a re-export of an existing
    period) can never truncate a previously-good billing CSV in place. Not 0600 — CDRs are
    meant to be read by NT's ingest, unlike the secret sidecar."""
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def pull_and_aggregate(
    registry: Registry,
    from_at: str | None = None,
    to_at: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Pull usage from every registered instance and aggregate per tenant (an instance's
    events are attributed to its tenant; multiple instances per tenant are merged).
    Strict: a failed instance pull aborts so a billing CDR is never silently partial."""
    pulled = pull_usage(registry, from_at, to_at, strict=True)
    instances = {instance["id"]: instance for instance in registry.list()}
    events_by_tenant: dict[str, list[dict[str, Any]]] = {}
    for instance_id, events in pulled.items():
        tenant = instances[instance_id]["tenant"]
        events_by_tenant.setdefault(tenant, []).extend(events)
    return aggregate_cdr(events_by_tenant, from_at, to_at)
