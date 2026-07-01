# Input Adapter & Return Path — Autonomous Spin Prompts

Ready-to-run prompts that drive an agent (Claude Code / Codex) to build **all** of
[../plans/input-adapter-return-path-plan.md](../plans/input-adapter-return-path-plan.md)
without stopping half-way, committing each milestone when its check is green.

> Same run-to-completion style as
> [ga-completion-spin-prompts.md](ga-completion-spin-prompts.md). The plan and
> [../specs/input-adapter-contract.md](../specs/input-adapter-contract.md) are the source
> of truth; these prompts only scope, sequence, and set the stop condition. Paste the
> **Shared Preamble** + **Master Driver** to run end-to-end, or run a single Stage block.

---

## Shared Preamble (the driver pastes/obeys this for every stage)

```text
Repo: /Users/seal/Documents/GitHub/atlas-control-plane
Start from a clean `main` with the existing completion gate passing. Requires Python 3.11+
(the code uses datetime.UTC).

Read FIRST, before editing (source of truth, in this order):
- docs/specs/input-adapter-contract.md            (the envelope + guarantees to implement)
- docs/plans/input-adapter-return-path-plan.md    (milestones IA-1, OB-1 + DoD + checks)
- docs/plans/ga-completion-plan.md                (house rules, migration + signing precedent)
- atlas/config.py, atlas/db.py, atlas/app.py, atlas/workflows.py, atlas/usage.py,
  atlas/thclaws_client.py, atlas/auth.py
- scripts/check_workflow_api.py, scripts/check_usage.py, scripts/check_migrations.py,
  scripts/check_silo.py   (copy their hermetic style: own temp DB, ephemeral port, mock thClaws)

Locate these anchors by SYMBOL (line numbers may have shifted post-GA — grep, do not trust
old numbers):
- Ingress dispatch: AtlasHandler._dispatch in atlas/app.py — the
  `workflow-triggers/{id}/fire` branch and the `POST /api/workflow-runs` branch
  (run input comes from payload/`input`).
- Trigger → input: TriggerService.fire_trigger in atlas/workflows.py — the whole payload is
  passed to start_workflow(definition_id, payload) and becomes the run input.
- OB-1 hook point: the runner's emission of the "workflow_run_completed" internal event
  (fire_internal(...)) in atlas/workflows.py — outbound delivery subscribes to the SAME
  completion, AFTER the run outcome is persisted.
- DB writers + migrations: create_workflow_run / create_artifact and the versioned
  migration runner in atlas/db.py — add the `deliveries` table as a NEW numbered migration
  step (WAL already on; do not redo it).
- HMAC signing precedent: the usage-export signer/verifier in atlas/usage.py — reuse the
  same HMAC-SHA256 primitive for X-Atlas-Signature.
- Outbound transport: atlas/thclaws_client.py already POSTs via urllib — reuse that stdlib
  transport for the outbound delivery (no new dependency).
- Audit writer + Config.from_env() (atlas/config.py) for the new ATLAS_OUTBOUND_* settings.

House rules (do NOT violate):
- Atlas core: Python standard library ONLY (urllib for outbound; ipaddress/socket for the
  SSRF guard). Dashboard: browser-native, no framework/build step. No new runtime dependency.
- All /api/* changes are ADDITIVE. Never change an existing path or response shape; every
  existing check script must keep passing.
- Silo: add NO `tenant_id` to any table. scripts/check_silo.py must stay green.
- BYOK/metering untouched: provenance + deliveries are visibility/operational only; never
  rate, bill, or touch budget_units.
- Secure defaults: ATLAS_OUTBOUND_ALLOWLIST empty = outbound DISABLED; never send unsigned
  (missing ATLAS_SECRET_KEY refuses to send); keep ATLAS_LOOPBACK_NO_AUTH bypass on
  127.0.0.1/::1 only.
- Delivery is a FAILURE-ISOLATED side effect (mirror usage metering): attempt only after the
  run outcome is persisted; a failed delivery can NEVER change a run's state.
- Every non-trivial behavior gets ONE hermetic runnable check under scripts/ and is appended
  to the completion gate. Never tick a DoD item early.

Completion gate (must stay green; append each stage's new check). Run at minimum:
  python3 -m py_compile atlas/config.py atlas/db.py atlas/app.py atlas/workflows.py \
    atlas/usage.py atlas/thclaws_client.py atlas/auth.py \
    scripts/check_input_adapter.py scripts/check_outbound.py
  node --check atlas/static/app.js
  python3 scripts/check_migrations.py
  python3 scripts/check_silo.py
  python3 scripts/check_workflow_api.py
  python3 scripts/check_usage.py
  python3 scripts/check_input_adapter.py     # new (IA-1)
  python3 scripts/check_outbound.py          # new (OB-1)
Then confirm the repo's FULL existing gate (every other scripts/check_*.py the repo already
ships) is still green from a clean tree.

Per-stage close-out (do this at the END of every stage, automatically):
1. Run the gate above + the full existing gate. Fix until green; do not advance while red.
2. DOCS SYNC (part of DoD): update every doc the change touches. Any /api/* change updates
   specs/openapi.yaml + specs/api-reference-en.md + specs/api-reference-th.md (EN+TH parity),
   tick the plan's progress ledger, confirm spec status lines, and keep docs/README.md links
   correct. scripts/check_docs.py must stay green.
3. Append a short note to PROGRESS.md.
4. Commit (do NOT push): `git add -A && git commit -m "<type>(<stage>): <summary>"`.
   Example: feat(IA-1): input adapter envelope + provenance audit.
5. Print: what changed, files touched, gate result, the new check, docs updated.
```

---

## Master Driver (paste after the Shared Preamble to run everything)

```text
GOAL: Execute docs/plans/input-adapter-return-path-plan.md to completion — both milestones,
in order, WITHOUT stopping between them.

Order:  IA-1  →  OB-1

For EACH stage:
  a. Implement to the stage's Definition of Done in the plan.
  b. Add the stage's hermetic check and append it to the completion gate.
  c. Run the full gate. If red, fix and re-run; do NOT advance until green.
  d. Docs sync + PROGRESS.md + COMMIT (no push) per the Shared Preamble close-out.
  e. CONTINUE to the next stage without waiting for confirmation.

SCOPE DISCIPLINE: do ONLY each stage's documented DoD. Do not build streaming per-artifact
delivery, per-trigger ingress HMAC storage, or any tenant_id/pooled-tenancy code — those are
recorded as External Decisions / out of scope in the plan.

Hard stops (the ONLY reasons to pause and ask the human):
- You would have to change an existing /api/* path or response shape.
- You would have to add a runtime dependency no stdlib path can replace.
- A DoD item genuinely cannot be met as specified.
Otherwise keep going until OB-1's check and the full gate are green from a clean tree, then
report: each stage's status, files touched, the commit list, and the External Decisions still
open (allowlist contents, streaming delivery, ingress HMAC, backoff tuning).
```

---

## Stage 1 — IA-1: Input Adapter Contract (envelope + provenance)  [Tier A]

```text
Follow input-adapter-return-path-plan.md §IA-1 and input-adapter-contract.md.

Implement:
- Parse the reserved `_meta` object from run input on BOTH ingress paths (fire_trigger
  payload→input, and POST /api/workflow-runs input). `_meta` is OPTIONAL; a payload without
  it must behave EXACTLY as today (backward compatible — this is the key regression to guard).
- Validate `_meta` when present: must be an object; source.channel ∈
  {line,email,web_form,api,schedule,other} (unknown → reject, fail closed); if
  reply.callback_url is present it must be a valid URL AND pass the outbound allowlist
  validator (write that validator now as a shared helper so OB-1 reuses it). Reject an
  invalid envelope with a clear error and CREATE NO RUN.
- Persist `_meta` with the run input (so OB-1 can read `_meta.reply`).
- On run start, write an audit entry with source.{channel,adapter,form,external_id} + run_id.
  Visibility only; never log a secret.

New check (append to gate): scripts/check_input_adapter.py — hermetic. Assert every bullet in
the plan's IA-1 check list: envelope on /fire and /workflow-runs records provenance and a
business field reaches a mock worker; a legacy payload without _meta still works end-to-end;
an invalid envelope is rejected pre-run; _meta.reply round-trips from the persisted run.

DoD + close-out per the plan and Shared Preamble. Then CONTINUE to OB-1.
```

---

## Stage 2 — OB-1: Outbound delivery (the return path)  [Tier A; needs IA-1]

```text
Follow input-adapter-return-path-plan.md §OB-1. Subscribe to the EXISTING
workflow_run_completed emission in the runner; do not invent a second completion path.

Implement:
- deliveries table via a NEW numbered migration step in atlas/db.py (no tenant_id):
  id, run_id, url, correlation_id, status{pending,delivered,failed,blocked}, attempts,
  max_attempts, last_error, created_at, updated_at, delivered_at. init() twice = no-op.
- Config in Config.from_env(): ATLAS_OUTBOUND_ALLOWLIST (empty ⇒ outbound disabled),
  ATLAS_OUTBOUND_MAX_ATTEMPTS (5), ATLAS_OUTBOUND_TIMEOUT (10). Signing requires
  ATLAS_SECRET_KEY; if unset, refuse to send (never unsigned).
- On workflow_run_completed (succeeded AND failed), if run input _meta.reply.mode=="webhook"
  and callback_url set, enqueue ONE delivery. Attempt only AFTER the run outcome is persisted;
  a delivery failure must never change the run state.
- SSRF guard (shared with IA-1 validation): require https (http only on loopback for dev);
  resolve host via socket.getaddrinfo; reject loopback/private/link-local/metadata IPs
  (ipaddress) unless the host matches ATLAS_OUTBOUND_ALLOWLIST; connect to the validated
  address. Blocked url ⇒ status=blocked, audited, never sent.
- Signed POST body {delivery_id, run_id, state, correlation_id, artifacts[], signed_at};
  artifacts carry key/kind/content (file_ref stays a pointer). Sign exact bytes with
  HMAC-SHA256 over ATLAS_SECRET_KEY (reuse usage.py signer) ⇒ header
  X-Atlas-Signature: sha256=<hex>. Transport via urllib (thclaws_client style).
- Bounded retries with backoff up to max_attempts, then status=failed (dead-letter, visible).
  delivery_id makes the receiver idempotent. On restart, pending deliveries may be re-attempted
  (bounded) — document that this differs from the never-auto-retry-worker-job rule because
  deliveries are Atlas-side idempotent notifications.
- Additive endpoints (RBAC): GET /api/deliveries?run_id=&status= (operator/auditor);
  POST /api/deliveries/{id}/retry (operator, bounded);
  POST /api/workflow-runs/{id}/deliver (operator, manual (re)send).

New check (append to gate): scripts/check_outbound.py — hermetic, with a mock receiver on an
ephemeral loopback port added to the allowlist. Assert the plan's OB-1 check list: signed POST
on completion with correct fields; non-allowlisted/private target ⇒ blocked, not sent; receiver
500 ⇒ bounded retries then failed while the run stays succeeded and a duplicate delivery_id is
not reprocessed; missing ATLAS_SECRET_KEY ⇒ refused; manual retry re-attempts within the bound.

DoD + close-out, incl. openapi.yaml + api-reference EN/TH for the new routes. This is the last
stage: finish with the full report.
```

---

## Order recap

```text
IA-1 (envelope + provenance) → OB-1 (signed outbound delivery + deliveries API)
```

Both are Tier A — build to full DoD with a green hermetic check. Commit each when green; push
only when the human asks.
