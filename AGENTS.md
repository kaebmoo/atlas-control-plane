# Repository Guidelines

## Purpose

Atlas is the control plane that coordinates thClaws workers: a deterministic workflow
engine, job routing/handoff, per-user auth/RBAC, usage metering, and an
instance-per-tenant Fleet. Atlas core (`atlas/`) is Python standard library only; the
dashboard (`atlas/static/`) is browser-native HTML/CSS/JS with no build step. Fleet
(`fleet/`) is a separate component with its own SQLite registry.

Authoritative docs (read before changing code; do not duplicate them here):
`docs/plans/ga-completion-plan.md` (§5 documentation policy), `docs/specs/`,
`docs/adr/`, `PROGRESS.md`, and the `docs/README.md` index.

## Working Rules

- Read existing documentation and tests before changing code. Preserve the existing
  architecture unless the task requires otherwise. Keep changes minimal and scoped.
- Atlas core is **Python standard library only** — no runtime dependencies. The
  dashboard uses **no framework or build step**.
- All `/api/*` changes are **additive**: never change an existing endpoint path or
  response shape. Existing clients and every check script must keep passing.
- Preserve the dashboard **gate-marker substrings** (element ids/classes asserted by
  `scripts/check_workflow_api.py`); never rename or remove them.
- Keep **EN + TH parity** for bilingual docs (api-reference, concepts, web-user-guide,
  visual-builder-spec). Any `/api/*` change updates `openapi.yaml` +
  `api-reference-en.md` + `api-reference-th.md`.
- **Silo invariant:** do not add `tenant_id` to `atlas/` core; pooled tenancy is
  deferred (see `docs/adr/0001-multi-tenancy-silo-vs-pooled.md`,
  guarded by `scripts/check_silo.py`).
- Never log, store, or return tokens or model keys. Keep the `ATLAS_LOOPBACK_NO_AUTH`
  dev bypass (loopback only) while shipping secure production defaults.
- Every non-trivial behavior gets one hermetic check under `scripts/` (own temp DB,
  ephemeral port, mock worker) folded into `scripts/gate.sh`.
- Do not modify generated files, vendored dependencies, or build artifacts. Do not
  suppress errors or weaken tests to make checks pass.

## Verification

Requires Python 3.11+ (the code uses `datetime.UTC`) and `node` (dashboard JS check).
Before reporting completion, run:

```bash
./scripts/gate.sh
```

If the gate cannot run, report the exact reason and run the closest available checks.

## Bug Audits

- Validate each finding against actual execution paths and tests; avoid speculative
  findings.
- Report file, line, impact, evidence, and reproduction steps.
- Distinguish confirmed bugs from risks that require further validation.
- Audits are read-only: do not modify source code unless the task explicitly asks.

## Workflow: adding code & hunting bugs

Three layers, distinct jobs. The **threat model** (`docs/specs/threat-model.md`) is the *oracle* —
out-of-scope items and accepted risks are **not bugs**. **codex / claude** are *samplers* — they
surface evidence; they cannot prove "zero bugs". `scripts/gate.sh` + `scripts/lint.sh` + branch
protection are the *deterministic backstop* — they block regressions on every PR. Never make a
sampler do the backstop's job ("audit until 0" never converges).

**Adding code:** branch off `main` → write it within the invariants above → give every non-trivial
behavior **one hermetic check** in `scripts/gate.sh` and **mutation-test it** (break the code; the
gate must go red — if it stays green the check is worthless) → `./scripts/gate.sh` + `./scripts/lint.sh`
green → PR → CI (`gate` + `lint`, required) → merge. Touch `/api/*` ⇒ also update `openapi.yaml` +
api-reference EN + TH. A new trust boundary or assumption ⇒ update the threat model.

**Hunting bugs:** scope it (one subsystem, a diff, or the trust-boundary matrix) — never "find all
bugs". Validate each finding against the real execution path before reporting (see Bug Audits). Fix
as a **class** (shared validator / safe-by-construction), not a point; then add a mutation-tested
hermetic check; then re-audit your own fix (first passes leave edge residue). For sign-off-level
claims use an **independent** reviewer — don't self-review. Low findings → `docs/specs/backlog.md`.

**Done (per release):** linters clean + stress/fuzz green + every fix mutation-locked + threat model
matches code. That is an **accepted baseline**, not a proof of bug-absence.
