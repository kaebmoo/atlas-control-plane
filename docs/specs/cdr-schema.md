# CDR Record Schema (PROPOSED — pending NT billing confirmation)

> TL;DR (ไทย): Fleet ดึง usage จากทุก instance แล้วรวมเป็น **CDR** (Charge Detail
> Record) CSV หนึ่งไฟล์ต่อหนึ่ง tenant ต่อหนึ่งช่วงเวลา (รายเดือน/รายปี ผ่าน `--from`/`--to`).
> นี่คือ **export อย่างเดียว** — ไม่มี rating engine, ไม่มี invoice, ไม่มี `tenant_invoices`
> (เรตและการออกบิลเป็นของทีม NT). โครงสร้างนี้ยัง **เป็นข้อเสนอ** รอ NT ยืนยัน จึงปั๊ก
> marker `x-schema: atlas.cdr.v1-proposed` ไว้บนหัวไฟล์. การ re-export ช่วงเดิมให้ผลเป็น
> ไบต์เดียวกัน (deterministic).

Fleet aggregates raw `usage_events` from each Atlas instance into a per-tenant,
per-period CDR. Atlas/Fleet **export** the CDR; NT's billing/mediation team owns rating
and invoicing.

## Status

This schema is **proposed**, not confirmed. Every CDR CSV begins with a marker line:

```
# x-schema: atlas.cdr.v1-proposed (PROPOSED - pending NT billing confirmation)
```

Confirm the final fields/units with the NT billing/mediation team (see the GA plan's
external-decision register).

## Record fields

One row per `(tenant, period, event_type)`.

| Field | Type | Meaning |
|---|---|---|
| `tenant` | string | Tenant the instance(s) serve. |
| `period_start` | ISO-8601 | Start of the billed period (the `--from` boundary). |
| `period_end` | ISO-8601 | End of the billed period (the `--to` boundary). |
| `event_type` | string | Usage event `kind` (e.g. `workflow_run`, `job`). |
| `count` | integer | Number of events of that type in the period. |
| `first_event_at` | ISO-8601 | Earliest event timestamp in the group. |
| `last_event_at` | ISO-8601 | Latest event timestamp in the group. |
| `budget_units` | integer (optional) | Sum of `units` for the group (billable units). |
| `seconds` | number (optional) | Sum of wall `seconds` for the group. |

The **billable unit is the successful workflow run** (per the usage-metering plan). The
`event_type = workflow_run` row counts **only successful runs** — failed/cancelled runs
are excluded from `count`, `budget_units`, and `seconds`, so the CDR never overstates the
billable quantity. A tenant with only failed runs produces no `workflow_run` row.

## Completeness

A CDR export is **all-or-nothing per run**: if any instance's usage pull fails (instance
down or HTTP error), the export aborts with an error rather than writing a partial,
complete-looking bill. Re-run once the instance is reachable.

## Determinism

Re-exporting the same period yields byte-identical files: tenants and rows are sorted
(`tenant`, then `event_type`), the field order is fixed, and the CSV contains **no
generated-at timestamp** — only timestamps derived from the data.

## Generating

```bash
python3 -m fleet cdr --from 2026-06-01 --to 2026-06-30 --out-dir ./cdr   # monthly
python3 -m fleet cdr --from 2026-01-01 --to 2026-12-31 --out-dir ./cdr   # annual
```

Writes one `cdr-<tenant>.csv` per tenant under `--out-dir`. Monthly vs annual is purely
the `--from`/`--to` range. See [fleet/README.md](../../fleet/README.md).

## Explicitly out of scope (NT owns these)

No rating engine, no price/tariff application, no invoices, no `tenant_invoices` table,
no ERP integration. The CDR is the hand-off artifact; NT rates and bills from it.
