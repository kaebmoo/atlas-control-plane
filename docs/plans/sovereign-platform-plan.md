# Atlas Sovereign Platform Plan (GA Gaps)

> TL;DR (ภาษาไทย): workflow engine เสร็จแล้ว เอกสารนี้คือ "ชั้น platform เชิงพาณิชย์"
> ที่ยังขาดเพื่อไปให้ถึง GA ตาม business plan — auth/RBAC, multi-tenancy,
> usage metering/billing, fleet provisioning, deployment hardening, solution packs.
>
> การตัดสินใจหลัก: **multi-tenant แบบ silo (1 control plane ต่อ 1 ลูกค้า)** ไม่ใช่
> pooled. แต่ละลูกค้าได้ Atlas instance ของตัวเอง (VM/DB/secret/URL แยก) และมี
> ชั้น **Atlas Fleet** อยู่ข้างบนไว้ provision/monitor/รวม usage แล้วส่งออกเป็น CDR
> ให้ระบบ billing ของ NT ไป rate/ออก invoice (เราไม่ออก invoice เอง).

This plan covers the commercial/platform layer needed to turn the current
single-tenant MVP into a sellable, governable, multi-customer product. It does
**not** re-cover the workflow engine, which is implemented and verified
(see [workflow-engine-plan.md](workflow-engine-plan.md) and
[workflow-engine-coding-plan.md](workflow-engine-coding-plan.md)). Usage pricing and
the metering/CDR billing model are detailed in
[usage-metering-billing-plan.md](usage-metering-billing-plan.md) (BYOK-first).

## Where the code is today

- Single SQLite file and single process (`ThreadingHTTPServer`), with per-user
  bearer tokens/RBAC plus legacy `ATLAS_API_TOKEN` bootstrap compatibility.
- Users, roles, and an idempotent usage ledger/export are per instance; there
  are still no pooled tenants or Atlas-side rating/invoicing.
- Worker `token` is authenticated ciphertext when `ATLAS_SECRET_KEY` is set;
  plaintext compatibility remains with an explicit warning when it is unset.
- Authenticated usernames are written to `audit_log.actor`.
- Effectively **already single-tenant**, which makes the silo model below cheap.

## Decision 1 — Multi-tenancy = Silo (instance-per-tenant), Fleet-managed

### Context / forces
- Target buyers: government, BFSI, SOE, healthcare in Thailand. Regulated.
- Sovereignty / data-residency / air-gap are the core value proposition; the
  business plan sells an explicit air-gapped "Sovereign Hosting Uplift" tier.
- Few, high-value customers (plan: 9 → 222 over 6 years; ACV ฿3M–฿20M), **not**
  high-volume SMB SaaS.
- Existing code is already single-tenant.

### Options
| Model | Isolation | Code change | Ops cost | Fit |
|---|---|---|---|---|
| **Pooled** (1 plane, many tenants, `tenant_id` + row scoping + cross-tenant RBAC) | Weak (logical) | Large rewrite | Low per tenant | ✗ wrong for regulated/air-gap buyer; noisy-neighbor & blast-radius risk |
| **Silo** (1 plane per tenant: own VM/DB/secrets/URL) | Strong (physical) | Minimal | Fleet ops + 1 VM/tenant | ✓ matches buyer, sovereignty, and existing code |
| **Hybrid** (silo for regulated; optional pooled tier later for SMB/dev) | Mixed | Silo now, pooled later | Medium | ✓ keep as future option |

### Decision
**Silo-first.** One Atlas instance per tenant. Build a thin **Atlas Fleet**
control layer above the silos. **Defer pooled tenancy** (only build if a
low-end shared tier is ever needed — YAGNI for now).

### Consequences
- Atlas core stays single-tenant → **no `tenant_id`, no cross-tenant code
  paths** (simpler, safer, easier to certify).
- Atlas core still needs, per instance: real **users + RBAC** (one agency has
  many operators), **usage metering export**, and **hardening**.
- New top component **Atlas Fleet**: instance registry, provisioning
  automation, health/version monitoring, usage aggregation → CDR export
  (NT billing rates/invoices, not us), fleet-wide upgrades. This is where "spin
  up as many control planes as you
  want" lives.
- Isolation is per deployment, so a tenant breach cannot reach another tenant.

### Target architecture (text)
```
                    ┌──────────────────────────────┐
                    │        Atlas Fleet           │  (new, runs at NT)
                    │  registry · provision · health│
                    │  usage aggregation · CDR export│
                    │  upgrade orchestration        │
                    └───────┬───────────┬───────────┘
        provision/health/usage pull (HTTPS, per-instance admin token)
            ┌───────────────┼───────────────┐
            ▼               ▼               ▼
   ┌────────────────┐ ┌────────────────┐ ┌────────────────┐
   │ Atlas (Tenant A)│ │ Atlas (Tenant B)│ │ Atlas (Tenant C)│  ← silo per tenant
   │ own VM/DB/secret│ │ own VM/DB/secret│ │ air-gapped       │
   │ users+RBAC      │ │ users+RBAC      │ │ (offline usage   │
   │ /api/usage      │ │ /api/usage      │ │  export by file) │
   │ thClaws workers │ │ thClaws workers │ │ thClaws workers  │
   └────────────────┘ └────────────────┘ └────────────────┘
```
Air-gapped tenants export usage as a signed file instead of a live pull.

## Decision 2 — Dependency policy at GA

Atlas core is stdlib-only today. Keep that where practical:
- Password hashing / token hashing: `hashlib.pbkdf2_hmac` + `secrets` + `hmac`
  (stdlib, no dependency).
- TLS: terminate at a reverse proxy (nginx/Caddy) — no Python TLS dependency.
- At-rest encryption of worker tokens: prefer an OS keychain / KMS / env-provided
  key. Allow **one** vetted dependency (`cryptography`) only if no keychain is
  available. Flag this as a per-deployment choice.

---

## Milestones (codeable, mirrors the existing coding-plan style)

Each milestone keeps the repo's "one runnable check" culture: add a
`scripts/check_*.py` that fails if the behavior breaks, and extend the
completion gate.

### M1 — Identity & Access (per instance)  ← GA blocker
- [x] `users` table: `id, username, password_hash, role, status, created_at, updated_at`.
- [x] `api_tokens` table: `id, user_id, token_hash, name, last_used_at, created_at, revoked_at` (store only the hash).
- [x] Roles: `admin`, `operator`, `viewer`, `auditor`. Define an RBAC matrix
      (who can run jobs, edit/run workflows, approve gates, manage workers,
      read audit, manage users).
- [x] Replace `AtlasHandler._is_authorized()` with: resolve token → user →
      role, then per-route permission check. Keep `ATLAS_LOOPBACK_NO_AUTH` for
      dev only; **default it to `false`** and require a token in prod config.
- [x] First-run bootstrap: create an `admin` + initial token via CLI/env
      (`python3 -m atlas.admin create-admin`), printed once.
- [x] Wire authenticated identity into the existing `audit_log.actor`.
- [x] Encrypt `workers.token` at rest (Decision 2). Add migration to re-encrypt.
- [x] Session cookie for the dashboard (login page) OR keep token-in-localStorage
      but issue per-user tokens. Recommend a simple login → session cookie.
- Check: `scripts/check_auth.py` — unauthorized request → 401; viewer cannot
  POST a job; admin can; revoked token rejected; audit records the actor.

### M2 — Usage metering & export  ← GA blocker
- [x] `usage_events` table: `id, idempotency_key UNIQUE, run_id, job_id,
      node_key, worker_id, actor, kind, status, units, seconds, started_at,
      finished_at, model, tokens_prompt, tokens_output, created_at, metadata`.
      `idempotency_key` (e.g. `job:<id>` / `run:<id>`) +
      `INSERT OR IGNORE` makes emission safe across retry/restart-recovery so a
      run is never double-counted.
- [x] Emit a usage event on job finish (jobs.py) and on workflow node
      completion / budget spend (workflows.py). Reuse `counters.budget_units_spent`.
- [x] Define the billable unit (configurable): **workflow-run count** (headline),
      job-run count, budget_units, and wall-clock seconds. Record all; let NT
      billing choose. Under **BYOK**, model/token counts are recorded for
      visibility only — never billed (see usage-metering-billing-plan.md).
- [x] `GET /api/usage?from=&to=&format=json|csv` (admin/auditor only).
- [x] Signed offline export for air-gapped tenants (file the Fleet can ingest),
      with `python3 -m atlas.usage export|verify`.
- Check: `scripts/check_usage.py` — mocked workflow, one event per job/run,
  counter totals, duplicate suppression, JSON/CSV RBAC, signed-file tamper
  detection, and failure isolation.

### M3 — Deployment hardening (per instance)  ← GA blocker
- [x] Production run mode: systemd unit + `scripts/run-prod.sh`; reverse proxy
      (TLS, gzip, request size limits) documented; `ATLAS_LOOPBACK_NO_AUTH=false`.
- [x] SQLite: enable WAL mode; scheduled `.backup` + restore runbook; document
      the single-writer caveat (fine at single-tenant scale).
- [x] **Versioned migrations**: add a `schema_version` table + ordered migration
      runner (current `CREATE TABLE IF NOT EXISTS` cannot evolve columns across
      a fleet). Required before fleet upgrades.
- [x] Config: secrets via env/secret store, not flags; configurable bind;
      structured request logging.
- Check: `scripts/check_migrations.py` — migrate an old DB snapshot forward
  cleanly and idempotently.

### M4 — Atlas Fleet (new component)  ← Phase 2, minimal slice in Phase 1
- [x] `instances` registry: `id, tenant, base_url, region, version,
      admin_token_ref, status, last_health_at, created_at`.
- [x] Provisioning via IaC (Terraform/cloud-init/Ansible) — **do not** build a
      bespoke orchestrator. Provide `atlas-fleet provision --tenant X` that:
      creates VM/container → deploys Atlas → runs migrations → seeds admin token
      → registers the instance. (Minimal slice: `python3 -m fleet provision` does
      local provisioning with rollback; VM-level IaC remains per-deployment.)
- [x] Health polling using the existing `/healthz`; version + drift reporting.
- [x] Upgrade orchestration: deploy new version + run M3 migrations per instance.
      (Migrations self-run on instance start; Fleet reports version drift.)
- [x] Usage pull from each instance `/api/usage` (or offline file ingest).
- Decision: Fleet can be its own small repo/service; it shares nothing with a
  tenant DB. Start with a CLI + registry; add a dashboard later.
- Check: provision → register → health-green → usage-pulled, against a local
  throwaway Atlas instance.

### M5 — Central usage aggregation & CDR export  ← Phase 2
- [x] Aggregate `usage_events` per tenant per period in the Fleet.
- [x] Emit a **CDR-style CSV** (one row per billable event, e.g. per
      workflow-run) and hand it to **NT's billing system** — same pattern as telco
      CDR. NT rates per plan tier and issues invoices, **not Atlas/Fleet**.
      (Schema is `atlas.cdr.v1-proposed`, pending NT billing confirmation —
      see docs/specs/cdr-schema.md.)
- [x] **No rating engine, no `tenant_invoices`, no ERP integration on our side.**
      Plan tiers are supplied to NT billing as config, not implemented here.
- See [usage-metering-billing-plan.md](usage-metering-billing-plan.md) Decision 3.
- Check: synthetic usage → one CDR file per tenant with correct per-period totals.

### M6 — Solution packs (Government first)  ← Phase 1 use case
- [x] Pack format: a JSON bundle `{ name, version, workflows[], triggers[],
      roles[], sample_input, docs }` (see docs/specs/pack-format.md; HMAC
      signing with `ATLAS_REQUIRE_SIGNED_PACKS` covers the M8-signing slice).
- [x] `GET /api/packs`, `POST /api/packs/import`, `GET /api/packs/{id}/export`.
- [x] First pack: **Citizen complaint intake → triage → response draft → human
      gate → publish** (atlas/packs/gov_complaint.json).
- Check: import pack → workflow definition + trigger created and validate-clean.

### M7 — Managed inference / model neutrality  ← later / optional tier
- Atlas is already model-agnostic (workers can run any model). A "Managed
  Inference" tier is mostly a **worker/runtime** concern, not Atlas. If a
  multi-provider gateway is wanted (OpenThaiGPT/Llama/Claude/GPT), build it as a
  dedicated gateway worker behind the existing worker abstraction — Atlas needs
  no change. Document, don't build yet.
- **Billing default is BYOK** (customer brings the model key, held by thClaws;
  Atlas never bills tokens). Managed Inference is the alternate SKU-B. See
  [usage-metering-billing-plan.md](usage-metering-billing-plan.md) Decision 0.

### M8 — Marketplace for community packs  ← later
- Depends on M6 pack format. A signed registry + ratings. Defer.

### M9 — Pooled multi-tenant tier  ← deferred (only if an SMB/shared tier appears)
- Would require `tenant_id` on every table + row scoping + per-tenant rate
  limits + cross-tenant RBAC. Explicitly **out of scope** under the silo
  decision. Listed so the decision is not silently reversed.

### Cross-cutting — Observability & compliance (woven into M1–M5)
- [x] Structured logs (`ATLAS_REQUEST_LOG`) + a metrics endpoint per instance
      (`GET /api/metrics`).
- [x] Per-tenant audit export (`GET /api/audit?from=&to=&format=csv`).
- [x] Data-classification tag on artifacts (`classification` →
      `metadata.classification`) + retention/purge policy
      (`python3 -m atlas.admin purge-artifacts`, docs/ops/deployment.md §6).
- [x] Secrets handling (M1) + backup encryption (`ATLAS_BACKUP_KEY` in
      scripts/backup.sh).
- Check: `scripts/check_observability.py`.

---

## Phasing aligned to the business plan

- **Phase 1 — GA blockers (toward 2027 GA):** M1 (auth/RBAC), M2 (metering +
  export), M3 (hardening + migrations), a **minimal M4** (provisioning runbook +
  instance registry, even if manual), and the **M6 government pack**.
  → enough to sell a governed, isolated, billable instance to the first agencies.
- **Phase 2 — Scale (2028+):** full M4 (automated fleet), M5 (central billing),
  M6 pack system breadth, M7 managed inference, M8 marketplace.
- **Phase 3 — ASEAN/HA:** multi-region fleet, HA per instance, M9 only if a
  shared tier is demanded.

## Open questions
- Auth: self-hosted users only, or SSO/OIDC against the tenant's IdP (gov often
  requires this)? SSO likely needed for BFSI/gov — may pull in a dependency.
- Provisioning target: which cloud/virtualization (GDCC, libvirt, k8s)? Decides
  the M4 IaC.
- Per-instance HA (the plan's later phases) vs. single-VM-with-backups for GA.
- ~~Billing unit of account~~ — **resolved**: subscription anchor + per
  *workflow-run* consumption; `budget_units` stays a cost guard, not a meter.
  See [usage-metering-billing-plan.md](usage-metering-billing-plan.md) Decision 2.

## Non-goals (first pass)
- Pooled multi-tenant isolation (M9).
- Building a custom infra orchestrator instead of IaC (M4).
- A model-provider gateway inside Atlas core (M7 lives in the worker layer).
- Marketplace (M8).
