# Atlas GA Completion Plan (run-to-completion)

> TL;DR (ภาษาไทย): เอกสารนี้คือ "แผนเดินจนจบจริง" สำหรับพา Atlas จากสถานะปัจจุบัน
> (engine M1–15 ✓, sovereign M1 auth ✓, M2 metering ✓) ไปจนครบทุกงานที่เหลือ —
> **M3 → M6 → M4 → M5/B3 → B2/B4 → M8 → B5 → M7/B7 → M9** บวก GA wrap.
> หลักการตามที่ตกลง: **เอาให้หมด** ตัวไหนโค้ดได้ก็ทำให้ถึง definition-of-done จริง
> ตัวไหนติดเหตุผลภายนอก (ทีม NT billing / thClaws / การตัดสินใจเชิงสถาปัตยกรรม)
> **ให้ทำโค้ดเท่าที่ทำได้ + ทำให้ระบบ "รองรับ" + เขียนเอกสารอธิบายเหตุผลและทางเดินต่อให้ละเอียด**
> ไม่ทิ้งงานค้างแบบไม่จบ. Commit เมื่อ gate เขียวจบแต่ละ milestone (ยังไม่ push).
>
> เอกสารแหล่งความจริง: [sovereign-platform-plan.md](sovereign-platform-plan.md) ·
> [usage-metering-billing-plan.md](usage-metering-billing-plan.md) ·
> [workflow-engine-coding-plan.md](workflow-engine-coding-plan.md). แผนนี้ **ไม่แทนที่**
> เอกสารเหล่านั้น แต่ลำดับและนิยาม "เสร็จ" ของงานที่ยังเหลือ และเป็นคู่กับชุด prompt
> [../prompts/ga-completion-spin-prompts.md](../prompts/ga-completion-spin-prompts.md).

This plan turns the remaining roadmap into a single, dependency-ordered run that an
agent (Claude Code) can execute end-to-end without stopping half-way. Every stage
has an explicit **Definition of Done (DoD)** and **one runnable check**, matching
the repo's existing "one check per milestone" culture.

---

## 1. Where we are (verified against code, 2026-06-29)

- Deterministic workflow engine: milestones **1–15 complete**.
- Sovereign **M1 (auth/RBAC)** complete — `atlas/auth.py`, `atlas/admin.py`,
  `scripts/check_auth.py`, per-user tokens, RBAC, worker-token encryption.
- Sovereign **M2 (usage metering + signed export)** complete — `usage_events`
  table (`atlas/db.py:199`), `atlas/usage.py`, `scripts/check_usage.py`,
  `GET /api/usage`, offline signed export/verify.
- **Already in place (do not redo):** `PRAGMA journal_mode = WAL` is enabled at
  `atlas/db.py:392`. An ad-hoc column migrator `_migrate()` exists at
  `atlas/db.py:404` (adds missing `jobs`/`approvals` columns). M3 must **formalize**
  this into a versioned runner, not re-invent WAL.

## 2. The finish line (scope), in three tiers

The agreed rule: **do everything; where something cannot be fully built for a real
reason, build the supporting code + readiness and document it thoroughly.** Each
remaining milestone is sorted into one of three tiers.

### Tier A — Build to full DoD (self-contained, codeable now)
`M3` hardening + versioned migrations · `M6` government solution pack ·
`M4` (minimal) Atlas Fleet registry + scripted provisioning + health + usage pull ·
`M5`+`B3` Fleet aggregation + CDR export (against a *proposed* schema) ·
`B2` dashboard Usage view · `B4` per-period quota alert · `M8` pack
signing/registry readiness.

### Tier B — Build to the boundary + readiness + docs (blocked by an external party)
`B5` BYOK key-injection helper — implement the env/config provisioning path
(option-b) now; define the forward interface for a future thClaws save-key endpoint
(option-a); never store the key in Atlas core. `M7`/`B7` managed inference — design
the gateway-worker + token/GPU-hour metering interface and document it; it lives in
the **worker/gateway layer, not Atlas core**, so core ships only the readiness doc.

### Tier C — ADR + readiness design, NOT a destructive core build
`M9` pooled multi-tenancy — write the silo-vs-pooled ADR, the exact migration/
change-list, and the explicit trigger to revisit. **Do not add `tenant_id` to core
now**; that would reverse the silo invariant every other milestone is built on.
(This is the honest answer to "why not M9 now": it is a deliberate product
decision, not a technical limit — see [§6](#6-why-m9-is-readiness-only-not-a-rewrite).)

> **Out of this plan's build scope but documented:** a full pooled-tenancy
> implementation. If NT ever signs off a shared/SMB tier, the M9 ADR is the
> ready-to-execute blueprint; it would become its own large track.

## 3. Dependency-ordered sequence (critical path)

```
Stage 1  M3   hardening + versioned migrations        ← linchpin; unblocks safe schema growth + Fleet
Stage 2  M6   government complaint pack                ← parallel-capable (uses finished engine); demo wedge
Stage 3  M4   minimal Fleet (registry/provision/health/usage)   ← needs M3 migrations
Stage 4  M5+B3 aggregation + CDR export                ← needs M2 + M4; CDR schema = documented assumption
Stage 5  B2+B4 Usage dashboard view + quota alert      ← needs M2
Stage 6  M8   pack signing + local registry readiness  ← needs M6 pack format
Stage 7  B5 + M7/B7 readiness (BYOK key, managed inference)   ← external dependency; boundary + docs
Stage 8  M9   pooled-tenancy ADR + migration design    ← decision doc; no core rewrite
Stage 9  GA wrap: security review + docs + full green gate
```

M3 is first because the versioned migration runner is a hard prerequisite for any
later schema change and for Fleet upgrades. M6 may run in parallel with M3 if
desired (it touches the API/UI/pack layer, not the migration core), but the
sequence above is the safe single-threaded order.

## 4. Definition of Done per stage

| Stage | Definition of Done | New check (added to gate) |
|---|---|---|
| ✅ **M3** | `schema_version` table + ordered idempotent migration runner in `db.py`; existing schema string and ad-hoc `_migrate()` folded into numbered steps; `scripts/backup.sh` + restore runbook; `scripts/run-prod.sh` + systemd unit + reverse-proxy/TLS doc; secure prod defaults (`ATLAS_LOOPBACK_NO_AUTH=false`); structured request logging. Old DB snapshot migrates clean and re-runs as a no-op. | `scripts/check_migrations.py` |
| ✅ **M6** | Pack bundle format `{name, version, workflows[], triggers[], roles[], sample_input, docs}` (versioned + signable); `GET /api/packs`, `POST /api/packs/import`, `GET /api/packs/{id}/export` (import reuses `create_workflow_definition` @db.py:721 + `create_workflow_trigger` @db.py:1079); first pack = **citizen complaint intake → triage → response draft → human gate → publish**; importing it validates clean and runs end-to-end on a mocked worker. | `scripts/check_packs.py` |
| ✅ **M4** | New `fleet/` component sharing nothing with a tenant DB; `instances` registry (id, tenant, base_url, region, version, admin_token_ref, status, last_health_at, created_at); `atlas-fleet` CLI: `provision` (deploy → run migrations → seed admin token → register), `list`, `health` (poll `/healthz`), `usage-pull` (`GET /api/usage` or ingest signed offline file); IaC via cloud-init/compose stub, not a bespoke orchestrator. Local throwaway instance: provision → register → health-green → usage pulled. | `fleet` check (e.g. `fleet/check_fleet.py`) |
| ✅ **M5+B3** | Proposed **CDR record schema** (tenant, period, event_type, count, first/last ts, optional budget_units/seconds) documented as *pending NT billing confirmation*; Fleet per-tenant-per-period aggregation → CDR CSV (monthly + annual via `from`/`to`); deterministic re-export; **no rating engine, no invoices, no `tenant_invoices`**. | `scripts/check_cdr.py` (or fleet-side) |
| ✅ **B2+B4** | Dashboard **Usage** view (runs/jobs/budget_units per period) reading `/api/usage`; per-period run-count **threshold alert** from the ledger (does **not** touch `budget_units`, which stays a cost guard); both preserve all gate markers. | extend `scripts/check_usage.py` |
| ✅ **M8** | Pack signing (HMAC/`ATLAS_SECRET_KEY`) + verify; tampered pack rejected; local pack registry listing; the public **marketplace service** documented as a future Fleet-side service (readiness, not built in core). | extend `scripts/check_packs.py` |
| **B5** | Write-only key-injection helper that writes the target worker's env/config as a provisioning step (option-b); forward interface defined for a future thClaws save-key endpoint (option-a); Atlas core stores **no** model key; injection is audited; key never appears in DB/logs/API responses. | `scripts/check_byok_helper.py` (fake target) |
| **M7/B7** | Gateway-worker design (multi-provider) + token/GPU-hour metering interface emitting extra CDR rows, documented as living in the worker/gateway layer; **no Atlas-core code**; readiness doc + interface spec only. | doc consistency (no core check) |
| **M9** | `docs/adr/0001-multi-tenancy-silo-vs-pooled.md`: decision, forces, the exact pooled change-list/migration path, risks, and the revisit trigger. Silo invariant intact — a grep proves no `tenant_id` was added to core tables. | grep assertion in CI note (no core code) |
| **GA wrap** | Security review checklist executed (auth, secrets, upload, SSRF to workers, RBAC); README + user guide + `docs/README.md` updated; **full completion gate green**; final report of what is built vs readiness-with-reason. | full gate + `/security-review` |

> Every row above **also** carries a docs deliverable: at its close-out it updates
> the docs its change touches and creates any new doc it introduces, per the
> Documentation policy in §5. "Done" is never code-only.

## 5. Execution contract (how "run to completion" must behave)

1. **Do not stop between milestones.** Execute stages in order; when one stage's
   DoD is met and the gate is green, **immediately continue** to the next.
2. **Commit per milestone, only when green.** After a stage's full gate passes,
   commit with a conventional message (`feat(M3): versioned migrations …`).
   **Do not `push`** unless explicitly asked.
3. **Never mark a stage done early.** A stage is done only when its DoD holds and
   the *entire* extended gate passes — not when code merely compiles.
4. **Blocked items still finish as readiness.** For Tier B/C, "done" =
   the supporting code that can be built + a thorough doc stating exactly what is
   blocked, why, the assumption taken, and the path to complete it. Record it; do
   not leave it silently unfinished.
5. **Keep a live progress ledger.** Maintain a checklist (tick items in this file
   and/or a `PROGRESS.md`) updated at each milestone so state is never ambiguous.
6. **Honor the house rules** (see the spin-prompts' Shared Preamble): Python
   stdlib only in core; browser-native HTML/CSS/JS only; all `/api/*` changes
   additive (no path/shape changes); preserve dashboard ids and gate-marker
   substrings; keep `ATLAS_LOOPBACK_NO_AUTH` dev bypass while shipping secure prod
   defaults; every behavior gets a hermetic check folded into the gate.
7. **No silent dependencies.** If a new runtime dependency seems necessary,
   implement the stdlib path. The only allowance is the plan's Decision 2
   (`cryptography`, only if no OS keychain) — and even then, document it; do not
   add it silently.
8. **Scope discipline — no overreach.** Do exactly each stage's documented DoD and
   nothing more. Never gold-plate, never pull work forward from another stage, never
   expand scope unilaterally. **Tier C (M9) and every "readiness" item are
   DOCUMENTATION/ADR ONLY — never write pooled-tenancy code, never add `tenant_id`
   to `atlas/` core, never alter existing table definitions for tenancy.** Tier B
   ships only the single boundary helper named in its stage; the rest is docs. If a
   stage looks like it needs more than its DoD, STOP and ask — do not decide to do
   more on your own.
9. **Docs stay in sync — every stage, not just at the end.** Updating documentation
   to match the code, and creating any new doc the stage introduces, is part of each
   stage's DoD and is enforced at close-out (see the Documentation policy below).

### Documentation policy — keep docs in lockstep with code

Documentation is updated **at every stage's close-out, before the commit** — never
deferred to the end. This mirrors the repo's existing rule ("update README, user
guide, examples … after the behavior and checks pass").

- **Update every doc the change touches.** Inventory to review each stage:
  `README.md` (API surface, status, config, project layout), `docs/README.md`
  (index + tree), `docs/architecture.md`, `docs/concepts-en.md` + `concepts-th.md`,
  `docs/thclaws-capability-matrix.md`, `docs/specs/api-reference-en.md` +
  `api-reference-th.md` + `openapi.yaml`, `docs/specs/*.schema.json`,
  `docs/specs/workflow-visual-builder-spec-en.md` + `-th.md`,
  `docs/guides/web-user-guide-en.md` + `-th.md`, `docs/workflow-examples.md`,
  `docs/demo-script.md`.
- **Any `/api/*` change updates all of:** `openapi.yaml`, `api-reference-en.md`,
  `api-reference-th.md`, and the relevant `*.schema.json` — kept consistent with the
  actual routes in `atlas/app.py`.
- **Bilingual parity is mandatory.** Docs that exist in EN + TH (api-reference,
  concepts, web-user-guide, visual-builder-spec) are updated in **both** languages;
  never ship the English side only.
- **Create the new docs each stage introduces** (required, not optional):
  - M3 → `docs/ops/deployment.md` (prod run + reverse proxy/TLS) and
    `docs/ops/backup-restore.md`.
  - M6 → `docs/specs/pack-format.md` (pack bundle schema).
  - M4 → `fleet/README.md` (registry + provisioning + usage-pull runbook).
  - M5/B3 → `docs/specs/cdr-schema.md` (proposed CDR schema, marked pending NT).
  - B5 → `docs/specs/byok-key-injection.md`; M7/B7 → `docs/specs/managed-inference.md`.
  - M9 → `docs/adr/0001-multi-tenancy-silo-vs-pooled.md`.
- **Update the index.** Every new doc is linked in `docs/README.md` (links + tree).
- **Docs-drift check (folded into the gate):** add `scripts/check_docs.py` that fails
  if a route in `atlas/app.py` is missing from `openapi.yaml` or either
  api-reference, or if a `docs/README.md` link points to a missing file.

## 6. Why M9 is readiness-only, not a rewrite

M9 (pooled multi-tenancy) is **not** technically impossible — it is deliberately
deferred by Decision 1 of the sovereign plan:

- **It contradicts the silo value proposition.** Target buyers (gov/BFSI/health,
  air-gap, data residency) treat *physical* per-tenant isolation as the feature;
  pooled `tenant_id` row-scoping is weaker isolation, harder to certify, and cannot
  be air-gapped per tenant.
- **It reverses a foundational invariant.** Every table and query in `atlas/` is
  built with **no `tenant_id`**. Retrofitting pooled tenancy means `tenant_id` on
  every table + row scoping on every query + cross-tenant RBAC + per-tenant rate
  limits — a large, invasive, high-risk rewrite touching all of M1–M8.
- **YAGNI for the target market.** The business plan sells few high-value logos
  (9→222, ACV ฿3–20M), not high-volume SMB SaaS. Pooled only pays off if a
  shared/SMB tier appears.

So per the agreed rule, M9 is delivered as a **complete ADR + migration blueprint +
readiness note** (the seam already exists: DB access is centralized in `db.py`).
It is ready to execute the day a shared tier is approved — without quietly
reversing the silo decision today.

## 7. External-decision register (assumptions taken so work proceeds)

These gate *full* completion of some stages but must not block the run. Each has a
default assumption the agent uses, clearly flagged for later confirmation.

| Open question | Affects | Default assumption taken | Confirm with |
|---|---|---|---|
| Exact CDR record schema | M5/B3 | Use the proposed schema in §4; mark CSV header `x-schema: proposed` | NT billing/mediation team |
| Auth: local users vs SSO/OIDC | M1 scope (done) / GA | Keep local users; document an OIDC extension point; do not pull a dependency now | NT IdP / security |
| BYOK key path (thClaws endpoint vs env) | B5 | Implement env/config injection (option-b); stub forward interface for option-a | thClaws team |
| Provisioning target (GDCC/libvirt/k8s) | M4 | Target docker-compose/systemd on a VM via cloud-init; note GDCC/k8s as alternates | NT infra |
| Per-instance HA vs single-VM+backup | GA | Single VM + scheduled `.backup` for GA; HA noted as Phase 3 | NT infra |

## 8. Canonical completion gate (extend per stage)

```bash
python3 -m py_compile atlas/config.py atlas/db.py atlas/app.py atlas/jobs.py \
  atlas/workflows.py atlas/router.py atlas/workflow_templates.py atlas/usage.py \
  atlas/auth.py atlas/admin.py \
  scripts/check_workflows.py scripts/check_workflow_api.py scripts/check_usage.py \
  scripts/check_auth.py
node --check atlas/static/app.js
python3 scripts/check_workflow_db.py
python3 scripts/check_workflows.py
python3 scripts/check_workflow_api.py
python3 scripts/check_auth.py
python3 scripts/check_usage.py
# + each new stage appends its check:
#   M3 → scripts/check_migrations.py
#   M6 → scripts/check_packs.py
#   M4 → fleet/check_fleet.py
#   M5/B3 → scripts/check_cdr.py
#   B5 → scripts/check_byok_helper.py
#   docs → scripts/check_docs.py (route↔openapi↔api-reference parity + index links)
```

> Requires Python **3.11+** (the code uses `datetime.UTC`). The maintainer's
> machine runs 3.14; a 3.10 environment will fail to import `db.py`.

## 9. Definition of "all done"

The run is complete when: every Tier A stage meets its DoD with a green gate and a
commit; every Tier B/C item has its readiness code (where any) + a thorough doc and
is recorded in the progress ledger; `docs/README.md`, `README.md`, and the user
guide reflect the new surfaces; the security-review checklist has been executed;
and the full completion gate passes from a clean tree.
