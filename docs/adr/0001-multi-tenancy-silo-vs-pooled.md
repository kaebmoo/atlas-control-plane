# ADR 0001 — Multi-tenancy: Silo (instance-per-tenant) vs Pooled

- **Status:** Accepted — silo. Pooled tenancy **deferred** (readiness blueprint below).
- **Date:** 2026-06-29
- **Deciders:** Atlas platform; reflects Decision 1 of the sovereign platform plan.

> TL;DR (ไทย): Atlas เลือก **silo — หนึ่ง instance ต่อหนึ่ง tenant** (แยก process/DB/host
> จริง) ไม่ใช่ pooled (หลาย tenant ใน DB เดียวด้วย `tenant_id`). เหตุผล: ลูกค้าเป้าหมายคือ
> หน่วยงานรัฐ/การเงิน/สุขภาพ ที่ต้องการ isolation เชิงกายภาพ + air-gap ต่อ tenant และทั้ง
> โค้ดเบสสร้างบนสมมุติฐาน "ไม่มี tenant_id". เอกสารนี้บันทึกการตัดสินใจ + **change-list ที่
> แม่นยำ** ถ้าวันหนึ่งต้องทำ pooled (เช่นมี tier SMB) + เส้นทาง migration + ความเสี่ยง +
> trigger ที่จะรื้อการตัดสินใจ. ตอนนี้ **ห้ามเพิ่ม tenant_id ใน atlas/ core** (มี
> `scripts/check_silo.py` กันไว้).

## Context and forces

- **Target buyers** are regulated and sovereignty-sensitive: government, BFSI, health,
  air-gapped sites, data-residency mandates. For them, **physical per-tenant isolation
  is a feature**, not an implementation detail — it is easier to certify and can be
  air-gapped per tenant.
- **The codebase assumes no tenant.** Every table in `atlas/db.py` and every query is
  written with **no `tenant_id`** and no row-scoping. Auth/RBAC, usage metering,
  workflows, approvals, artifacts, and the dashboard all assume a single tenant per
  instance.
- **Business model.** The plan sells a small number of high-value logos (ACV ฿3–20M),
  not high-volume SMB SaaS. Pooled multi-tenancy only pays off if a shared/SMB tier
  appears.
- **The seam already exists.** All DB access is centralized in `atlas/db.py` (the
  `Database` class), and Fleet (`fleet/`) already models one instance per tenant
  (`instances.tenant`) and aggregates per-tenant usage/CDR across instances. So tenant
  separation lives **above** Atlas core today.

## Decision

Adopt **silo: one Atlas instance per tenant** (separate process, SQLite DB, and host/
container), orchestrated by Fleet. **Do not** introduce pooled `tenant_id` row-scoping
into Atlas core now.

Rationale: it matches the isolation buyers demand, avoids a large invasive rewrite of a
working M1–M8 surface, and is YAGNI for the current market. Pooled tenancy is deferred,
not rejected — this ADR is its ready-to-execute blueprint.

## Consequences

- **Positive:** strongest isolation (per-tenant process/DB/host, air-gap-able);
  simplest core (no scoping bugs = no cross-tenant data-leak class); per-tenant backup/
  restore and upgrade; Fleet already provisions and meters per tenant.
- **Negative / accepted:** higher per-tenant overhead (one process + DB each); not
  economical for many small tenants; cross-tenant analytics must be aggregated in Fleet
  (already the M5/B3 CDR design), not queried from one DB.

## Invariant and its guard

Atlas core contains **no `tenant_id`**. This is enforced by
[`scripts/check_silo.py`](../../scripts/check_silo.py) (in the completion gate), which
fails if `tenant_id` appears in any `atlas/*.py`. Verified at this ADR's writing:

```
$ grep -rn "tenant_id" atlas/ --include="*.py"   # → no matches
```

Adding `tenant_id` to core is therefore a deliberate, gated act that requires reversing
this ADR — never an accident.

---

## Pooled-tenancy blueprint (only if the revisit trigger fires)

If a shared/SMB tier is approved, pooled tenancy becomes its **own large track**. The
exact change-list:

### 1. Schema — `tenant_id` on every tenant-scoped table
Add `tenant_id TEXT NOT NULL` (+ a `tenants` table, + composite indexes/uniqueness per
tenant) to: `users`, `api_tokens`, `workers`, `workspaces`, `conversations`,
`session_bindings`, `jobs`, `job_events`, `usage_events`, `workflow_definitions`,
`workflow_runs`, `workflow_nodes`, `workflow_edges`, `workflow_events`, `approvals`,
`artifacts`, `workflow_triggers`, `workflow_trigger_events`, `audit_log`. Existing
`UNIQUE` constraints (e.g. `users.username`, `workers.base_url`,
`usage_events.idempotency_key`) become **per-tenant** uniqueness. This is a new set of
versioned migration steps (the M3 runner already supports append-only steps).

### 2. A single DB-access scoping layer
The seam exists: `atlas/db.py` centralizes access. Introduce a tenant context (a
`contextvar`, mirroring the existing `_AUDIT_ACTOR` pattern) and have **every** read/
write filter and stamp `tenant_id`. No query may omit the tenant predicate — enforce
with a query helper, not ad-hoc SQL.

### 3. Cross-tenant RBAC
Add a tenant dimension to identity: each user/token belongs to a tenant; a new
platform-operator role may span tenants for support. `_is_authorized` and
`_required_permission` (`atlas/app.py`) resolve the caller's tenant and scope every
request to it; reject cross-tenant access.

### 4. Per-tenant rate limits and quotas
Add per-tenant request/rate limits and per-tenant usage quotas (the B4 threshold alert
generalizes to per-tenant). Today there is one global instance per tenant; pooled needs
explicit fairness controls.

### 5. Per-tenant export scoping in Fleet
Fleet aggregation/CDR (`fleet/cdr.py`) currently attributes one instance to one tenant.
Pooled means one instance hosts many tenants, so usage pull and CDR export must scope by
`tenant_id` **within** an instance (the CDR is already per-tenant, so the change is the
source query, not the output shape).

### Staged migration path
1. Land the `tenants` table + `tenant_id` columns as nullable, backfilled to a default
   tenant (existing silo data → one tenant). 2. Add the scoping layer behind a flag,
   defaulting to the single backfilled tenant (no behavior change). 3. Make `tenant_id`
   `NOT NULL` + per-tenant uniqueness once backfill is verified. 4. Enable cross-tenant
   RBAC + rate limits. 5. Allow provisioning multiple tenants into one instance.

### Risks
- **Cross-tenant data leak** is now a live class of bug (a single missing predicate);
  silo has none. Requires exhaustive query-scoping review + tests.
- **Certification/air-gap** story weakens — may disqualify Atlas for the very buyers it
  targets; pooled must be an additional tier, never a replacement for silo.
- **Blast radius:** one instance now serves many tenants (noisy-neighbor, shared outage,
  shared backup/restore).
- Touches all of M1–M8; high-risk, high-effort.

### Test strategy
- A hermetic cross-tenant isolation suite: seed two tenants, assert no API/DB path of
  tenant A ever returns tenant B's rows (users, tokens, jobs, runs, artifacts, usage,
  audit). 2. Migration test: a silo snapshot backfills to one tenant and behaves
  identically. 3. RBAC: cross-tenant access denied; platform-operator scoping explicit.
  4. Per-tenant quota/rate-limit tests. Replace `scripts/check_silo.py` with a
  pooled-isolation gate.

## Revisit trigger

Reopen this ADR **only** when there is a **signed-off business case for a shared / SMB
tier** (NT product + security). Absent that, silo stands and `tenant_id` stays out of
core.
