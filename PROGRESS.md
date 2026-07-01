# Atlas GA Completion — Progress Ledger

Tracks the run-to-completion stages in
[docs/plans/ga-completion-plan.md](docs/plans/ga-completion-plan.md). One line per
stage at close-out (gate green + docs synced + committed).

| Stage | Status | Notes |
|---|---|---|
| M3 — migrations + hardening | ✅ done | Versioned migration runner (`schema_version` + ordered idempotent steps) folding old `_migrate()`; `scripts/backup.sh`, `run-prod.sh`, example systemd unit; `ATLAS_REQUEST_LOG` JSON request log; secure defaults confirmed; `scripts/check_migrations.py` added to gate; ops docs added. |
| M6 — government pack | ✅ done | `atlas/packs.py` (validate/import/export, reuses workflow + trigger validators); additive `/api/packs`, `/api/packs/import`, `/api/packs/{id}/export` (RBAC: read / workflows.manage); `atlas/packs/gov_complaint.json` (intake→triage→draft→human gate→publish) runs end-to-end on a mock worker; `scripts/check_packs.py` in gate; `docs/specs/pack-format.md` + openapi + api-reference EN/TH. |
| M4 — Atlas Fleet | ✅ done | New `fleet/` (own SQLite registry, no shared tenant DB, no tenant logic in core); `atlas-fleet` CLI provision/list/health/usage-pull; admin token by `admin_token_ref` + 0600 secrets sidecar (never raw token in registry/logs); compose IaC stub + systemd alt. Added additive unauthenticated `GET /healthz` (`{ok,service,version}`) to atlas core for health probes. `fleet/check_fleet.py` in gate (provision→register→health→usage-pull→offline). Docs: `fleet/README.md`, openapi + api-reference EN/TH (/healthz). |
| M5+B3 — CDR export | ✅ done | `fleet/cdr.py`: aggregate raw usage per tenant/period → deterministic CDR CSV (one file per tenant), `x-schema: atlas.cdr.v1-proposed` marker; `python3 -m fleet cdr --from --to --out-dir` (monthly+annual). Export only — no rating/invoices. `scripts/check_cdr.py` in gate (row counts, schema columns, byte-identical re-export). Doc: `docs/specs/cdr-schema.md`. |
| B2+B4 — usage view + alert | ✅ done | Dashboard **Usage** view (index.html/app.js/styles.css): from/to controls, run/job/budget totals from `/api/usage`, authenticated JSON/CSV blob downloads (no token in URL), gated to admin/auditor. B4 read-only run-count threshold alert (`usage_threshold_alert` in usage.py; client mirror) that never touches budget_units. All gate-marker substrings preserved; verified live in-browser (totals + tripped alert). `check_usage.py` extended; user-guide EN+TH updated. Codex 1×P2+1×P3 fixed (token-in-URL → blob fetch; load on restored view). |
| M8 — pack signing | ✅ done | `sign_pack`/`verify_pack_signature` (HMAC-SHA256 over canonical bundle, `ATLAS_SECRET_KEY`); import verifies a present signature (tampered/wrong-key/no-key rejected), unsigned accepted unless `require_signature`; `python3 -m atlas.packs sign/verify` CLI; `signed` flag in listing. Marketplace = readiness doc (Fleet-side, not core). `check_packs.py` extended; pack-format.md + openapi + api-reference EN/TH updated. |
| B5 + M7/B7 — BYOK / inference readiness | ✅ done | B5: `atlas/byok.py` write-only key injection (option-b env file 0600), audited, key never in Atlas DB/logs/responses; CLI reads key from `$ATLAS_BYOK_KEY` (never an arg); option-a (thClaws save-key) interface defined as a documented stub. `scripts/check_byok_helper.py` in gate (asserts key absent from DB file). M7/B7: `docs/specs/managed-inference.md` — gateway-worker + token/GPU-hour metering emits extra CDR rows; lives in worker/gateway layer, no Atlas-core change. Doc: `docs/specs/byok-key-injection.md`. |
| M9 — pooled-tenancy ADR | ✅ done | `docs/adr/0001-multi-tenancy-silo-vs-pooled.md`: silo decision, exact pooled change-list (tenant_id on every table, scoping layer, cross-tenant RBAC, per-tenant limits, Fleet export scoping), staged migration + risks + test strategy, revisit trigger. No `tenant_id` in core — proven and guarded by `scripts/check_silo.py` (in gate). Docs/ADR only; zero core/tenant code. |
| GA wrap — security + docs + green gate | ✅ done | Full-surface security review (per-stage codex + a holistic `codex review --base main`): auth/RBAC on every new route, no plaintext key/token in logs (request log path-only)/DB (BYOK key absent; fleet token by ref + 0600)/responses, `/healthz` leaks nothing, ATLAS_SECRET_KEY signing, BYOK no-key-in-core. 3 pack findings fixed (import reference validation; version round-trip; non-string role → clean 400). Canonical gate `scripts/gate.sh` green from a clean tree; docs/README links all resolve. |

## Input Adapter & Return Path

Tracks [docs/plans/input-adapter-return-path-plan.md](docs/plans/input-adapter-return-path-plan.md).

| Milestone | Status | Notes |
|---|---|---|
| IA-1 — envelope + provenance | ✅ done | Reserved `_meta` (`source`/`reply`) parsed/validated at the single `WorkflowRunner.start_workflow` choke point shared by both ingress paths (`/api/workflow-triggers/{id}/fire` and `POST /api/workflow-runs`); legacy payloads without `_meta` unaffected. `_meta.source` audited (`workflow_run.provenance`) against the run_id. New `atlas/outbound.py` (`resolve_outbound_target`): stdlib-only SSRF/allowlist guard for `reply.callback_url` (empty `ATLAS_OUTBOUND_ALLOWLIST` = disabled by default), shared as-is by OB-1. `scripts/check_input_adapter.py` added to gate. Docs: contract status updated, plan DoD ticked. |
| OB-1 — outbound delivery | ☐ not started | Signed return-path delivery on `workflow_run_completed`; `deliveries` table + API. |

## External confirmations still outstanding

- **CDR record schema** — proposed (`x-schema: atlas.cdr.v1-proposed`); confirm fields/units with NT billing/mediation.
- **thClaws save-key endpoint** — BYOK option-a is a documented stub; blocked on thClaws shipping the endpoint (option-b env injection works now).
- **Provisioning target** — default docker-compose/systemd on a VM; GDCC/k8s noted as alternates (NT infra).
- **Auth SSO/OIDC** — local users now; OIDC is a documented extension point (NT IdP).

## Security findings

No open findings. Everything codex surfaced across the run was fixed and guarded by a hermetic check (see per-stage rows). The one intentional unauthenticated route, `GET /healthz`, returns only `{ok, service, version}`.
