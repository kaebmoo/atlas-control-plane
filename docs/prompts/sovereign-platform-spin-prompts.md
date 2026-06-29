# Atlas Sovereign Platform — Implementation Spin Prompts

Ready-to-run prompts for implementing
[../plans/sovereign-platform-plan.md](../plans/sovereign-platform-plan.md).

Run **one milestone per session**. Paste the **Shared Preamble** followed by one
**Milestone** block. The plan file is the source of truth; these prompts just
scope and sequence the work.

---

## Shared Preamble (paste before every milestone prompt)

```text
Repo: /Users/seal/Documents/GitHub/atlas-control-plane
Start from a clean `main` with the completion gate passing.

Before editing:
- Verify the branch is main and the working tree is clean.
- Read, in this order:
  - docs/plans/sovereign-platform-plan.md   (the plan — source of truth)
  - atlas/config.py, atlas/app.py, atlas/db.py, atlas/jobs.py, atlas/workflows.py
  - scripts/check_workflow_api.py, scripts/check_workflows.py, scripts/check_workflow_db.py
- Understand how app.py dispatches routes (segment matching on the path parts),
  how AtlasHandler._is_authorized() works, and how Config.from_env() loads settings,
  BEFORE changing them.

House rules (do not violate):
- Atlas core: Python standard library ONLY. No new runtime dependency unless the
  plan's "Decision 2" explicitly allows it. If you believe one is required, STOP
  and ask before adding it.
- Dashboard: browser-native HTML/CSS/JS only. No framework, no build step.
- All /api/* changes are ADDITIVE. Do not change existing endpoint paths or
  response shapes. Existing clients and check scripts must keep working.
- Preserve dashboard element ids and the document-level click-delegation classes.
  The gate asserts exact substrings in atlas/static/index.html and app.js —
  keep all of: workflowPolicyForm, explainWorkflowBtn, repairWorkflowBtn,
  suggestWorkersBtn, id="workflowTemplateSelect", id="retryInterruptedRunBtn",
  syncPolicyFormFromJson, "Validated repair copied", applyWorkerSuggestion,
  toggleTrigger, template.graph, template.policy, retry_interrupted: true.
- Keep a working dev mode: ATLAS_LOOPBACK_NO_AUTH must still bypass auth on
  127.0.0.1/::1 for local development. Ship SECURE defaults for production.
- Every non-trivial behavior gets ONE runnable check under scripts/ and is added
  to the completion gate. Checks must be hermetic: own temp DB, ephemeral port,
  no reliance on external workers (mock thClaws like the existing checks do).
- Do NOT commit or push unless explicitly asked.

Completion gate (must stay green; append your new check):
  python3 -m py_compile atlas/config.py atlas/db.py atlas/app.py atlas/jobs.py atlas/workflows.py atlas/router.py atlas/workflow_templates.py scripts/check_workflows.py scripts/check_workflow_api.py
  node --check atlas/static/app.js
  python3 scripts/check_workflow_db.py
  python3 scripts/check_workflows.py
  python3 scripts/check_workflow_api.py

After completion: summarize what changed, list changed files, report the gate
plus your new check, note remaining limitations, and show git status.
Do not commit unless asked.
```

---

## Milestone M1 — Identity & Access (per instance)  [GA blocker — start here]

```text
Goal: replace the single shared bearer token with real users, roles, and
per-user API tokens, scoped to this single-tenant instance, without breaking any
existing API, check script, or the dashboard. Follow docs/plans/sovereign-platform-plan.md §M1.

Implement:
- DB (additive; match the existing CREATE TABLE IF NOT EXISTS style in db.py):
  - users(id, username UNIQUE, password_hash, role, status, created_at, updated_at)
  - api_tokens(id, user_id, token_hash, name, last_used_at, created_at, revoked_at)
  - roles: admin, operator, viewer, auditor
- Hashing with stdlib only: passwords via hashlib.pbkdf2_hmac (with per-user salt),
  tokens stored as a hash; compare with hmac.compare_digest; generate with secrets.
  Never store or log a raw token/password.
- Rewrite AtlasHandler._is_authorized() to: take the Bearer token -> look up
  api_tokens by hash -> load user + role -> update last_used_at. Return 401 for
  missing/invalid/revoked tokens. Keep loopback bypass when ATLAS_LOOPBACK_NO_AUTH
  is true and the client is 127.0.0.1/::1. If the legacy ATLAS_API_TOKEN env is
  set, accept it as a bootstrap ADMIN token (backward compatibility).
- RBAC: define a single permission matrix (one dict) and add a per-route check.
  Suggested: viewer=read-only; operator=run jobs/workflows + approve gates;
  auditor=read audit+usage; admin=everything incl. user/token management.
  Return 403 on insufficient role. Enforce on the server regardless of UI.
- CLI: python3 -m atlas.admin with subcommands create-admin / create-user /
  create-token / revoke-token / list-users. create-admin prints a one-time token.
- Wire the authenticated username into the existing audit_log.actor column
  (currently defaults to 'local').
- Worker token at rest: encrypt workers.token transparently at the db read/write
  boundary WITHOUT a schema change — store ciphertext with a short marker prefix,
  read legacy plaintext once and re-encrypt on next write. Take the key from
  ATLAS_SECRET_KEY (env/secret store). If unset, keep current behavior and log a
  clear warning (per Decision 2).
- New endpoints (additive, RBAC-guarded): POST /api/auth/login (username+password),
  POST /api/auth/logout, GET /api/me, and admin-only CRUD under /api/users and
  /api/tokens.
- Dashboard (atlas/static/*): add a minimal login screen shown when the SPA gets
  a 401; reuse the existing localStorage atlasApiToken flow for the per-user token
  (or set a session cookie). Add a small "signed in as <user> (<role>)" + Sign out
  control in the sidebar foot. Keep every existing id/class and all gate markers.
  Role-gate UI actions progressively, but rely on server enforcement as the truth.

Must not break:
- The existing check scripts run on loopback with ATLAS_LOOPBACK_NO_AUTH=true, so
  keep loopback permissive in dev. Add enforced-auth coverage in a NEW check
  rather than changing the existing ones.

New check (add to the gate): scripts/check_auth.py — hermetic (temp DB, ephemeral
port; simulate a non-loopback client or disable loopback bypass). Assert:
- no token -> 401; invalid token -> 401; revoked token -> 401
- viewer POST /api/jobs -> 403; operator POST /api/jobs -> 2xx
- admin creates a user + token; that token authenticates; audit_log.actor shows
  the acting username
- legacy ATLAS_API_TOKEN authenticates as admin
- a worker saved with ATLAS_SECRET_KEY set stores ciphertext (not plaintext) and
  still polls/reads back correctly

Deliverables, process, and completion gate per the Shared Preamble.
```

---

## Milestone M2 — Usage metering & export  [GA blocker]

```text
Run after M1. Goal: record billable usage and expose an export the Fleet can
ingest, as a pure side effect that never affects job/workflow outcomes.

Read BOTH as the source of truth, and implement EXACTLY what they specify — do not
invent a schema or billable unit in this prompt:
- docs/plans/sovereign-platform-plan.md §M2 — usage_events table, billable units,
  /api/usage, offline export.
- docs/plans/usage-metering-billing-plan.md — §(ก) "How the metering schema will be
  done", Decision 2 (billable unit), Decision 3 (CDR / no rating engine here).

Key requirements to honor (from those docs):
- DB (additive): usage_events as defined in §M2, INCLUDING `idempotency_key UNIQUE`
  (e.g. job:<id> / run:<id>) so emission is safe across retry/restart-recovery via
  INSERT OR IGNORE — a run/job is never double-counted.
- Emit a usage_event when a job finishes (atlas/jobs.py) and on workflow node
  completion / budget spend (atlas/workflows.py; reuse counters.budget_units_spent).
  Record all measures from §M2: workflow-run count (headline unit), job-run count,
  budget_units, wall_seconds.
- Under BYOK, model/token counts are recorded for VISIBILITY ONLY — never billed.
- A metering failure MUST be caught and logged, never propagated — it cannot fail
  or alter a job/run. Do not change any existing response shape.
- GET /api/usage?from=&to=&format=json|csv — admin/auditor only (RBAC from M1).
- Offline export: a signed JSON file (HMAC with ATLAS_SECRET_KEY) for air-gapped
  tenants, plus a verify helper.
- Do NOT build a rating engine or invoices here — that is NT billing's job; M5 only
  aggregates and exports CDR (billing-plan Decision 3).

New check (add to the gate): scripts/check_usage.py — run a mocked-worker workflow;
assert one usage_event per job AND one per workflow-run, totals equal the run
counters, re-emitting the same job/run does NOT create duplicates (idempotency),
CSV parses, and /api/usage enforces RBAC.

Deliverables, process, and completion gate per the Shared Preamble.
```

---

## Milestone M3 — Deployment hardening & migrations  [GA blocker]

```text
May run first if you prefer a migration runner before schema growth. Follow
docs/plans/sovereign-platform-plan.md §M3.

Implement:
- Versioned migrations: add a schema_version table and an ordered, idempotent
  migration runner in db.py. Fold existing table creation and the new M1/M2
  tables into numbered steps. Running twice must be a no-op.
- SQLite: enable WAL mode; add scripts/backup.sh (online .backup) and a restore
  runbook; document the single-writer caveat (acceptable at single-tenant scale).
- Production: scripts/run-prod.sh + an example systemd unit; document a reverse
  proxy for TLS; secure config defaults (ATLAS_LOOPBACK_NO_AUTH=false, token
  required). Add structured request logging.

New check (add to the gate): scripts/check_migrations.py — migrate an empty DB
and an older snapshot forward; assert a clean, idempotent re-run and the expected
final schema_version.

Deliverables, process, and completion gate per the Shared Preamble.
```

---

## M4–M9 (later)

Derive each prompt the same way from the plan:
- **M4 Atlas Fleet** — a NEW component/repo. Provision instances via IaC
  (Terraform/cloud-init/Ansible); do NOT build a bespoke orchestrator. Start with
  an `instances` registry + `atlas-fleet provision --tenant X` + `/healthz`
  polling + usage pull.
- **M5 Central usage aggregation & CDR export** — aggregate usage per tenant/period
  in the Fleet and export a **CDR-style CSV to NT's billing system** (NT rates per
  plan tier and issues invoices; Atlas/Fleet does **not** build a rating engine,
  `tenant_invoices`, or ERP integration). See usage-metering-billing-plan.md Decision 3.
- **M6 Government solution pack** — first revenue use case (citizen complaint
  intake → triage → response draft → human gate → publish). Define the pack
  bundle format + import/export endpoints.
- **M7 managed inference** — deferred. Billing default is **BYOK** (customer brings
  the model key, held by thClaws; Atlas never bills tokens); Managed Inference is the
  alternate SKU-B in the worker/gateway layer. Read usage-metering-billing-plan.md
  Decision 0 & B7 before building.
- **M8 marketplace**, **M9 pooled tenancy** — deferred; see the plan's non-goals
  before starting either.
