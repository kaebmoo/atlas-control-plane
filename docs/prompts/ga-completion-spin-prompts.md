# Atlas GA Completion — Autonomous Spin Prompts

Ready-to-run prompts that drive an agent (Claude Code) to finish **all** remaining
work in [../plans/ga-completion-plan.md](../plans/ga-completion-plan.md) **without
stopping half-way**, committing each milestone when its gate is green.

> Unlike [sovereign-platform-spin-prompts.md](sovereign-platform-spin-prompts.md)
> (one milestone per session, no commit), this set is a **run-to-completion driver**.
> Paste the **Shared Preamble** + the **Master Driver** to run everything end-to-end.
> The per-stage blocks below are what the driver executes in order; you can also run
> any single block on its own.

The plan file is the source of truth. These prompts only scope, sequence, and set
the stop condition.

---

## Shared Preamble (the driver pastes/obeys this for every stage)

```text
Repo: /Users/seal/Documents/GitHub/atlas-control-plane
Start from a clean `main` with the completion gate passing. Requires Python 3.11+
(the code uses datetime.UTC; a 3.10 env fails to import atlas/db.py).

Read FIRST, before editing (source of truth, in this order):
- docs/plans/ga-completion-plan.md              (the run-to-completion plan + DoD)
- docs/plans/sovereign-platform-plan.md         (silo decision, M-milestones)
- docs/plans/usage-metering-billing-plan.md     (BYOK, CDR, B-milestones)
- atlas/config.py, atlas/db.py, atlas/app.py, atlas/jobs.py, atlas/workflows.py,
  atlas/usage.py, atlas/auth.py, atlas/admin.py
- scripts/check_workflow_api.py, scripts/check_workflows.py,
  scripts/check_workflow_db.py, scripts/check_auth.py, scripts/check_usage.py

Know these anchors before changing them:
- Route dispatch: AtlasHandler.do_GET @app.py:88, do_POST @:91, _dispatch @:103
  (urlparse(self.path) then segment matching on `parts`; e.g. workflow-triggers @:549+).
- Auth: AtlasHandler._is_authorized() (token -> api_tokens hash -> user -> role).
- Config: Config.from_env().
- DB: schema SQL string @db.py:74-391; init() @:399; ad-hoc _migrate() @:404
  (PRAGMA table_info column adds for jobs @:405, approvals @:417);
  PRAGMA journal_mode = WAL ALREADY enabled @:392 (do NOT redo WAL).
- DB writers to reuse: create_workflow_definition @:721, create_workflow_run @:789,
  create_workflow_trigger @:1079, create_artifact @:1228.
- Usage: summarize_usage @usage.py:48, usage_csv @:70, create_signed_usage_export
  @:81, verify_signed_usage_export @:106, CLI main @:167.

House rules (do NOT violate):
- Atlas core: Python standard library ONLY. Dashboard: browser-native HTML/CSS/JS,
  no framework/build step. If you think a runtime dependency is required, implement
  the stdlib path instead; the only allowance is the plan's Decision 2
  (`cryptography`, only if no OS keychain) and even then DOCUMENT it — never add a
  dependency silently.
- All /api/* changes are ADDITIVE. Never change existing endpoint paths or response
  shapes. Existing clients and all check scripts must keep passing.
- Preserve dashboard element ids and document-level click-delegation classes. The
  gate asserts exact substrings in atlas/static/index.html and app.js — keep all of:
  workflowPolicyForm, explainWorkflowBtn, repairWorkflowBtn, suggestWorkersBtn,
  id="workflowTemplateSelect", id="retryInterruptedRunBtn", syncPolicyFormFromJson,
  "Validated repair copied", applyWorkerSuggestion, toggleTrigger, template.graph,
  template.policy, retry_interrupted: true.
- Keep dev mode: ATLAS_LOOPBACK_NO_AUTH must still bypass auth on 127.0.0.1/::1.
  Ship SECURE defaults for production (no bypass by default).
- Every non-trivial behavior gets ONE hermetic runnable check under scripts/ (own
  temp DB, ephemeral port, mock thClaws like the existing checks) and is appended to
  the completion gate.

Completion gate (must stay green; append each stage's new check):
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
  # + new per stage: check_migrations.py, check_packs.py, fleet/check_fleet.py,
  #   check_cdr.py, check_byok_helper.py, check_docs.py

Per-stage close-out (do this at the END of every stage, automatically):
1. Run the FULL gate above (with your new check appended). Fix until green. Do not
   advance while red. Never tick a DoD item early.
2. DOCS SYNC (part of DoD, not optional) — per the Documentation policy in
   docs/plans/ga-completion-plan.md §5: update every doc the change touches and
   create any new doc the stage introduces. Any /api/* change updates openapi.yaml +
   api-reference-en.md + api-reference-th.md (+ the relevant *.schema.json), kept
   consistent with atlas/app.py routes. Keep EN+TH parity (never the English side
   only). Link new docs in docs/README.md (links + tree).
3. Update the progress ledger: tick the stage in docs/plans/ga-completion-plan.md
   §4 and append a short note to PROGRESS.md (create it if missing).
4. Commit (do NOT push): `git add -A && git commit -m "<type>(<stage>): <summary>"`.
   Example: feat(M3): versioned migrations + prod hardening.
5. Print: what changed, files touched, gate result, the new check, docs updated, and
   anything left as readiness-with-reason.
```

---

## Master Driver (paste this after the Shared Preamble to run everything)

```text
GOAL: Execute docs/plans/ga-completion-plan.md to completion — all stages, in order,
WITHOUT stopping between them. This is a run-to-completion job, not one-milestone.

Loop over the stages in this exact order, each using its block below:
  M3  →  M6  →  M4  →  M5+B3  →  B2+B4  →  M8  →  B5  →  M7/B7  →  M9  →  GA wrap

For EACH stage:
  a. Implement to the stage's Definition of Done in the plan (§4).
  b. Add/extend the stage's hermetic check and append it to the completion gate.
  c. Run the FULL gate. If red, fix and re-run; do NOT advance until green.
  d. Sync docs (per the Documentation policy), update the progress ledger, and COMMIT
     (no push) per the Shared Preamble close-out.
  e. Immediately CONTINUE to the next stage. Do not wait for confirmation.

Tier rules (from the plan §2):
- Tier A stages (M3, M6, M4, M5+B3, B2+B4, M8): build to FULL DoD with a green check.
- Tier B (B5, M7/B7) and Tier C (M9): these are blocked by an external party or by
  the silo decision. "Done" for them = the supporting code that CAN be built + a
  THOROUGH doc that states exactly what is blocked, why, the assumption taken, and
  the path to finish it. Build the readiness; never leave it silently unfinished.

SCOPE DISCIPLINE (no overreach) — do ONLY each stage's documented DoD; never
gold-plate or pull work forward from another stage. M9 and every Tier C/"readiness"
item is DOCS/ADR ONLY: never write pooled-tenancy code, never add `tenant_id` to
`atlas/` core, never change an existing table definition or any /api/* path/shape.
If a stage looks like it needs more than its DoD, STOP and ask — do not expand scope.

Hard stops (the ONLY reasons to pause and ask the human):
- You would have to change an existing /api/* path or response shape to proceed.
- You would have to add a runtime dependency that no stdlib path can replace.
- A stage's DoD genuinely cannot be met AND cannot be expressed as readiness+docs.
Otherwise: keep going until the GA-wrap stage passes and the full gate is green from
a clean tree. End with a final report: every stage's status (done vs
readiness-with-reason), the commit list, and remaining external confirmations.
```

---

## Stage 1 — M3: Deployment hardening + versioned migrations  [Tier A, linchpin]

```text
Follow ga-completion-plan.md §4 (M3) and sovereign-platform-plan.md §M3.

Implement:
- Versioned migrations in atlas/db.py: add a `schema_version` table and an ORDERED,
  IDEMPOTENT migration runner. Fold the existing CREATE TABLE IF NOT EXISTS schema
  string (db.py:74-391) and the ad-hoc _migrate() column-adds (db.py:404; jobs @:405,
  approvals @:417) into numbered, append-only steps. Running init() twice must be a
  no-op. An older DB snapshot (pre-schema_version) must migrate forward cleanly:
  detect current version from existing tables/columns, then apply only missing steps.
- WAL is already on (db.py:392) — keep it; do not duplicate. Add scripts/backup.sh
  using SQLite online `.backup`, plus a restore runbook in docs; document the
  single-writer caveat (acceptable at single-tenant scale).
- Production: scripts/run-prod.sh + an example systemd unit file; document a reverse
  proxy for TLS/gzip/request-size limits; secure defaults (ATLAS_LOOPBACK_NO_AUTH
  defaults false in prod; token required). Add structured (JSON or key=value)
  request logging behind a config flag, without changing response shapes.

New check (append to gate): scripts/check_migrations.py — hermetic. Assert:
- a fresh DB initializes to the expected final schema_version;
- init() run twice is a no-op (no duplicate columns/tables, version unchanged);
- an older snapshot (build one by creating tables WITHOUT schema_version + without a
  late column) migrates forward to the final version with all expected columns.

DoD + close-out per the plan and Shared Preamble. Then CONTINUE to M6.
```

---

## Stage 2 — M6: Government complaint solution pack  [Tier A, demo wedge]

```text
Follow ga-completion-plan.md §4 (M6) and sovereign-platform-plan.md §M6.

Implement:
- Pack bundle format (versioned + signable):
  { name, version, schema_version, workflows[], triggers[], roles[], sample_input,
    docs, signature? }. Define it once and document it in docs/specs/.
- Endpoints (additive, RBAC-guarded; add to _dispatch @app.py:103 following the
  existing `parts ==` style):
    GET  /api/packs                 (list importable/installed packs)
    POST /api/packs/import          (validate then create definitions+triggers)
    GET  /api/packs/{id}/export     (serialize back to a bundle)
  Import must REUSE existing writers: create_workflow_definition @db.py:721 and
  create_workflow_trigger @db.py:1079, and run the existing workflow graph validator
  (do not bypass validation). Roles map to existing RBAC roles only.
- First pack file (e.g. atlas/packs/gov_complaint.json): citizen complaint intake →
  triage (worker) → response draft (worker) → human_gate (approve/reject) → publish.
  Include a realistic sample_input and short docs.

New check (append to gate): scripts/check_packs.py — hermetic. Assert:
- importing the gov pack creates the workflow definition(s) + trigger(s) and they
  pass the normal validator (validate-clean);
- export round-trips to an equivalent bundle;
- a run from sample_input completes end-to-end on a MOCK worker (reach the human
  gate, approve, finish);
- an invalid pack (bad node/edge/role) is rejected with a clear error.

DoD + close-out. Then CONTINUE to M4.
```

---

## Stage 3 — M4: Minimal Atlas Fleet (registry + provisioning)  [Tier A; needs M3]

```text
Follow ga-completion-plan.md §4 (M4) and sovereign-platform-plan.md §M4. Atlas Fleet
is a NEW component that shares NOTHING with a tenant DB. Put it under fleet/ (its own
module + its own small SQLite registry); do NOT add tenant logic to atlas/ core.

Implement:
- `instances` registry: id, tenant, base_url, region, version, admin_token_ref,
  status, last_health_at, created_at. admin_token_ref is a reference/handle, never
  the raw token in plaintext logs.
- `atlas-fleet` CLI (stdlib argparse): 
    provision --tenant X  → deploy an Atlas instance (via cloud-init/compose stub or
                            a local subprocess for the check), run its migrations,
                            seed an admin token (python3 -m atlas.admin create-admin),
                            register it.
    list                  → show instances + status/version.
    health                → poll each instance GET /healthz; update status/last_health_at.
    usage-pull --from --to→ GET /api/usage from each instance (or ingest a signed
                            offline file) and store/print the raw events.
- Provisioning uses IaC stubs (cloud-init/docker-compose), NOT a bespoke orchestrator.
  Default target documented: docker-compose/systemd on a VM (GDCC/k8s noted as alt,
  per the plan's external-decision register).

New check (append to gate): fleet/check_fleet.py — hermetic. Spin a local throwaway
Atlas (ephemeral port, temp DB, loopback no-auth or seeded token), then assert:
provision → register → health green → usage-pull returns the instance's events.

DoD + close-out. Then CONTINUE to M5+B3.
```

---

## Stage 4 — M5 + B3: Central aggregation + CDR export  [Tier A; CDR schema = assumption]

```text
Follow ga-completion-plan.md §4 (M5+B3), sovereign-platform-plan.md §M5, and
usage-metering-billing-plan.md Decision 3 + B3. Aggregation/CDR lives in fleet/
(or atlas/usage-side export helpers), NOT as Atlas-core rating.

Implement:
- A PROPOSED CDR record schema (documented as pending NT billing confirmation):
  tenant, period_start, period_end, event_type, count, first_event_at, last_event_at,
  optional budget_units, optional seconds. Emit a header/marker noting it is proposed.
- Fleet aggregation: pull usage_events per instance (reuse summarize_usage
  @usage.py:48 patterns), aggregate per tenant per period, emit a CDR-style CSV
  (reuse usage_csv @usage.py:70 style). Support monthly AND annual ranges via from/to.
  Re-export of the same period must be deterministic (stable ordering, same bytes).
- DO NOT build a rating engine, invoices, tenant_invoices, or ERP integration — NT
  billing rates/invoices. We only export the CDR.

New check (append to gate): scripts/check_cdr.py (or fleet-side) — hermetic. Feed
synthetic multi-instance usage; assert one CDR file per tenant, row count == billable
workflow-runs for the period, columns match the proposed schema, and a re-export is
byte-identical (deterministic).

DoD + close-out. Then CONTINUE to B2+B4.
```

---

## Stage 5 — B2 + B4: Usage dashboard view + quota alert  [Tier A]

```text
Follow ga-completion-plan.md §4 (B2+B4) and usage-metering-billing-plan.md B2/B4.

Implement:
- B2: a dashboard "Usage" view (atlas/static/index.html + app.js + styles.css) that
  reads the existing GET /api/usage and shows workflow-runs / jobs / budget_units per
  period, with from/to controls and JSON/CSV download links. New ids/classes only —
  do NOT remove or rename any existing gate-marker id/class.
- B4: a per-period run-count THRESHOLD alert derived from the ledger (e.g.
  "X% of expected monthly volume used"), surfaced in the Usage view. This must NOT
  touch budget_units, which stays the per-run cost guard. The threshold is config or
  per-period input, computed read-only from usage_events.

New check (extend scripts/check_usage.py): assert the data the Usage view needs is
served (period totals correct via the existing endpoint), the threshold alert fires
when synthetic run volume crosses it, and budget_units guard behavior is unchanged.
Keep node --check atlas/static/app.js green and all gate-marker substrings intact.

DoD + close-out. Then CONTINUE to M8.
```

---

## Stage 6 — M8: Pack signing + local registry readiness  [Tier A; needs M6]

```text
Follow ga-completion-plan.md §4 (M8) and sovereign-platform-plan.md §M8 (deferred
marketplace). Build the SIGNING + LOCAL REGISTRY readiness; the public marketplace
SERVICE stays a documented future Fleet-side component.

Implement:
- Pack signing: sign a pack bundle with HMAC using ATLAS_SECRET_KEY (reuse the usage
  export signing approach @usage.py:81/106). Verify on import; reject a tampered pack.
- A local pack registry listing (installed/available packs with name/version/signed).
- Document the future marketplace (signed registry + ratings) as readiness in
  docs/ — explicitly NOT built in core now, with the reason and the extension path.

New check (extend scripts/check_packs.py): a signed pack verifies and imports; a
tampered pack is rejected; an unsigned pack is handled per policy (documented).

DoD + close-out. Then CONTINUE to B5.
```

---

## Stage 7 — B5 + M7/B7: BYOK key helper + managed-inference readiness  [Tier B; external]

```text
Follow ga-completion-plan.md §4 (B5, M7/B7) and usage-metering-billing-plan.md
Decision 0, B5, B7. These are blocked by thClaws/worker-layer gaps — build the
boundary + readiness, document the rest.

Implement (B5 — what CAN be built now):
- A WRITE-ONLY key-injection helper (CLI and/or Fleet action) that writes the target
  worker's env/config so thClaws can load the model key (option-b). Atlas CORE stores
  NO model key. Define and document the forward interface for option-a (forward to a
  future thClaws save-key endpoint) so it drops in later.
- Audit the injection action (actor, target worker, timestamp) — but NEVER log,
  store, or return the key value. 

Document (M7/B7 — readiness only, NOT Atlas-core code):
- docs/: the managed-inference gateway-worker design (multi-provider behind the
  existing worker abstraction) and a token/GPU-hour metering interface that emits
  EXTRA CDR rows at the gateway layer (rated by NT billing, not us). State clearly it
  lives in the worker/gateway layer; Atlas core needs no change (per plan M7).

New check (append to gate): scripts/check_byok_helper.py — hermetic, against a FAKE
config/worker target. Assert the key is written to the target, the action is audited,
and the key value never appears in the Atlas DB, logs, or any API response.

DoD = B5 helper works for option-b + audited; M7/B7 = design+interface doc, marked
out-of-core readiness. Close-out. Then CONTINUE to M9.
```

---

## Stage 8 — M9: Pooled-tenancy ADR + migration design  [Tier C; readiness only]

```text
Follow ga-completion-plan.md §6 and sovereign-platform-plan.md Decision 1 + M9.
DO NOT add tenant_id to atlas/ core. This stage is a DECISION + BLUEPRINT, not a
rewrite — pooled tenancy would reverse the silo invariant the whole codebase relies on.

Produce docs/adr/0001-multi-tenancy-silo-vs-pooled.md containing:
- Context/forces (regulated/air-gap buyers; existing no-tenant_id core).
- Decision: silo (instance-per-tenant), pooled deferred.
- The EXACT pooled change-list if ever approved: tenant_id on every table, a single
  DB-access scoping layer (note db.py already centralizes access — the seam exists),
  cross-tenant RBAC, per-tenant rate limits, per-tenant export scoping in Fleet.
- A staged migration path + risks + test strategy.
- The explicit REVISIT TRIGGER: a signed-off shared/SMB tier business case.

Verify the silo invariant is intact: grep the core for `tenant_id` and confirm it is
absent from atlas/ table definitions. Record this check in the ADR.

DoD = ADR + blueprint committed, silo invariant proven intact. Close-out. CONTINUE to GA wrap.
```

---

## Stage 9 — GA wrap: security review + docs + green gate  [Tier A]

```text
Final stage. Do not finish until this passes.

Do:
- Run a security review of the full surface (use the /security-review skill if
  available, else a manual pass): auth/RBAC enforcement on every new route, secret
  handling (ATLAS_SECRET_KEY, no plaintext tokens/keys in logs/DB/responses), upload
  safety, request-size limits, SSRF considerations on worker calls, and the BYOK
  no-key-in-core guarantee. Fix findings or record them with severity + plan.
- Update docs: README.md (new endpoints/surfaces), docs/README.md (link the new
  plan + this prompt file + any new specs/ADR), the user guide, and PROGRESS.md.
- Run the ENTIRE completion gate from a clean tree; it must be green.

Final report: per-stage status (DONE vs READINESS-WITH-REASON), the full commit list,
the external confirmations still outstanding (CDR schema, thClaws key endpoint,
provisioning target, SSO), and any security findings. Do NOT push unless asked.
```

---

## Order recap

```text
M3 (migrations/hardening) → M6 (gov pack) → M4 (fleet) → M5+B3 (CDR) →
B2+B4 (usage UI/alert) → M8 (pack signing) → B5+M7/B7 (BYOK/inference readiness) →
M9 (pooled ADR) → GA wrap (security + docs + green gate)
```

Tier A = built to full DoD with a green check. Tier B (B5, M7/B7) and Tier C (M9) =
supporting code where possible + thorough readiness docs, never left silently
unfinished. Commit each stage when green; push only when the human asks.
