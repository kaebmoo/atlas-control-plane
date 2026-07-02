from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import io
import json
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

from .config import Config
from .db import Database, atomic_write_text, now_iso


USAGE_EXPORT_SCHEMA = "atlas.usage.v1"
USAGE_CSV_FIELDS = (
    "id",
    "idempotency_key",
    "kind",
    "status",
    "units",
    "seconds",
    "run_id",
    "job_id",
    "node_key",
    "worker_id",
    "actor",
    "started_at",
    "finished_at",
    "model",
    "tokens_prompt",
    "tokens_output",
    "created_at",
    "metadata",
)


def normalize_usage_range(from_at: str | None, to_at: str | None) -> tuple[str | None, str | None]:
    raw_from = _boundary_dt(from_at, end=False)
    raw_to = _boundary_dt(to_at, end=True)
    # Order the RAW endpoints, before snapping. A sub-second-wide-but-valid interval
    # (from=..:00.1Z < to=..:00.9Z) is legitimate and must yield an empty result set — snapping
    # legitimately inverts it (from ceils to ..:01Z, to floors to ..:00Z), which the query
    # returns as zero rows; it is NOT a client error.
    if raw_from and raw_to and raw_from > raw_to:
        raise ValueError("usage from must not be after to")
    return _snap(raw_from, end=False), _snap(raw_to, end=True)


def summarize_usage(events: list[dict[str, Any]]) -> dict[str, int | float]:
    run_events = [event for event in events if event.get("kind") == "workflow_run"]
    job_events = [event for event in events if event.get("kind") == "job"]
    return {
        "workflow_runs": len(run_events),
        "successful_workflow_runs": sum(event.get("status") == "succeeded" for event in run_events),
        "jobs": len(job_events),
        "budget_units": sum(int(event.get("units") or 0) for event in run_events),
        "wall_seconds": round(sum(float(event.get("seconds") or 0) for event in run_events), 6),
        "job_wall_seconds": round(sum(float(event.get("seconds") or 0) for event in job_events), 6),
    }


def usage_threshold_alert(
    events: list[dict[str, Any]],
    expected_runs: int,
    threshold_ratio: float = 0.8,
) -> dict[str, Any]:
    """Per-period run-count threshold alert (B4): read-only from usage_events. Reports how
    much of the expected workflow-run volume has been used and whether it crossed the
    threshold. Deliberately does NOT consider budget_units — that stays the per-run cost
    guard; this is a volume signal only."""
    used = summarize_usage(events)["workflow_runs"]
    expected = int(expected_runs or 0)
    ratio = (used / expected) if expected > 0 else 0.0
    return {
        "expected_runs": expected,
        "used_runs": used,
        "ratio": round(ratio, 6),
        "threshold_ratio": threshold_ratio,
        "alert": expected > 0 and ratio >= threshold_ratio,
    }


def elapsed_seconds(started_at: str | None, finished_at: str | None) -> float | None:
    if not started_at or not finished_at:
        return None
    try:
        return max(0.0, (_parse_timestamp(finished_at) - _parse_timestamp(started_at)).total_seconds())
    except ValueError:
        return None


def _csv_safe(value: Any) -> Any:
    """Neutralize CSV/spreadsheet formula injection: a cell whose text starts with = + - @
    (or a leading control char a spreadsheet may strip back to one) is executed as a formula
    by Excel/Sheets. Prefix such values with a single quote so they import as literal text.
    Non-strings pass through unchanged."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r", "\n"):
        return "'" + value
    return value


def usage_csv(events: list[dict[str, Any]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=USAGE_CSV_FIELDS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for event in events:
        row = dict(event)
        row["metadata"] = json.dumps(row.get("metadata") or {}, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        writer.writerow({field: "" if row.get(field) is None else _csv_safe(row.get(field)) for field in USAGE_CSV_FIELDS})
    return output.getvalue()


AUDIT_CSV_FIELDS = ["id", "created_at", "actor", "action", "resource_type", "resource_id", "details"]


def audit_csv(entries: list[dict[str, Any]]) -> str:
    """CSV export of audit entries (per-tenant audit export for compliance hand-off).
    Same formula-injection hygiene as usage_csv."""
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=AUDIT_CSV_FIELDS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for entry in entries:
        row = dict(entry)
        row["details"] = json.dumps(row.get("details") or {}, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        writer.writerow({field: "" if row.get(field) is None else _csv_safe(row.get(field)) for field in AUDIT_CSV_FIELDS})
    return output.getvalue()


def create_signed_usage_export(
    db: Database,
    secret_key: str,
    from_at: str | None = None,
    to_at: str | None = None,
) -> dict[str, Any]:
    if not secret_key:
        raise ValueError("ATLAS_SECRET_KEY is required to sign a usage export")
    from_at, to_at = normalize_usage_range(from_at, to_at)
    events = db.list_usage_events(from_at, to_at)
    payload = {
        "schema": USAGE_EXPORT_SCHEMA,
        "generated_at": now_iso(),
        "from": from_at,
        "to": to_at,
        "totals": summarize_usage(events),
        "usage": events,
    }
    return {
        "algorithm": "HMAC-SHA256",
        "payload": payload,
        "signature": _signature(payload, secret_key),
    }


def verify_signed_usage_export(export: dict[str, Any], secret_key: str) -> bool:
    if not secret_key or export.get("algorithm") != "HMAC-SHA256":
        return False
    payload = export.get("payload")
    signature = export.get("signature")
    if not isinstance(payload, dict) or payload.get("schema") != USAGE_EXPORT_SCHEMA or not isinstance(signature, str):
        return False
    return hmac.compare_digest(signature, _signature(payload, secret_key))


def write_signed_usage_export(
    db: Database,
    path: Path,
    secret_key: str,
    from_at: str | None = None,
    to_at: str | None = None,
) -> dict[str, Any]:
    export = create_signed_usage_export(db, secret_key, from_at, to_at)
    # Atomic: re-exporting to the same path must never truncate a prior signed export on crash.
    atomic_write_text(path, json.dumps(export, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n")
    return export


def verify_signed_usage_export_file(path: Path, secret_key: str) -> bool:
    try:
        export = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(export, dict) and verify_signed_usage_export(export, secret_key)


def _signature(payload: dict[str, Any], secret_key: str) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(secret_key.encode("utf-8"), encoded, hashlib.sha256).hexdigest()


def _boundary_dt(value: str | None, end: bool) -> datetime | None:
    """Parse a usage/audit range endpoint (an ISO-8601 date or timestamp) to a UTC datetime,
    unsnapped. A bare date maps to the start (from) or end (to) of that day."""
    value = str(value or "").strip()
    if not value:
        return None
    if len(value) == 10:
        try:
            day = datetime.fromisoformat(value).date()
        except ValueError as exc:
            raise ValueError("usage range must use an ISO-8601 date or timestamp") from exc
        return datetime.combine(day, time.max if end else time.min, tzinfo=UTC)
    return _parse_timestamp(value)  # already normalized to UTC


def _snap(boundary: datetime | None, end: bool) -> str | None:
    """Snap a parsed boundary to a whole second in its inclusive direction and format it ...SSZ.
    Stored timestamps are second-resolution (now_iso() truncates microseconds) and neither string
    nor julianday() comparison resolves sub-second boundaries reliably (julianday is a float in
    days: ~microsecond deltas collapse). So a `to` upper bound floors (keeps the whole-second row
    it lands on) and a `from` lower bound with any fractional part ceils to the next second (so the
    preceding whole-second row, strictly before the boundary, is excluded)."""
    if boundary is None:
        return None
    if boundary.microsecond and not end:
        try:
            boundary = boundary.replace(microsecond=0) + timedelta(seconds=1)
        except OverflowError:
            # `from` is in the last representable second with sub-second precision: the next whole
            # second overflows datetime, and nothing can satisfy the lower bound. Surface a 400
            # (ValueError) rather than letting OverflowError escape as a 500.
            raise ValueError("usage from is past the maximum representable timestamp") from None
    return boundary.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except ValueError as exc:
        raise ValueError("usage range must use an ISO-8601 date or timestamp") from exc
    except OverflowError:
        # An extreme but valid offset (e.g. 0001-...+14:00 or 9999-...-14:00) can push the instant
        # outside datetime's representable range when converted to UTC. Surface a 400, not a 500.
        raise ValueError("usage range timestamp is outside the representable range") from None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Export or verify signed Atlas usage files")
    subparsers = parser.add_subparsers(dest="command", required=True)
    export_parser = subparsers.add_parser("export", help="write a signed usage JSON file")
    export_parser.add_argument("output", type=Path)
    export_parser.add_argument("--db", type=Path)
    export_parser.add_argument("--from", dest="from_at")
    export_parser.add_argument("--to", dest="to_at")
    verify_parser = subparsers.add_parser("verify", help="verify a signed usage JSON file")
    verify_parser.add_argument("input", type=Path)
    args = parser.parse_args(argv)

    config = Config.from_env()
    if not config.secret_key:
        parser.error("ATLAS_SECRET_KEY is required")
    if args.command == "export":
        db = Database((args.db or config.db_path).resolve(), secret_key=config.secret_key)
        write_signed_usage_export(db, args.output.resolve(), config.secret_key, args.from_at, args.to_at)
        print(args.output.resolve())
        return
    if not verify_signed_usage_export_file(args.input.resolve(), config.secret_key):
        raise SystemExit("usage export signature is invalid")
    print("usage export signature is valid")


if __name__ == "__main__":
    main()
