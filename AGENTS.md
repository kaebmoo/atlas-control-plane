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
