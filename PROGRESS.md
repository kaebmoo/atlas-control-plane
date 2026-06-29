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
| B5 + M7/B7 — BYOK / inference readiness | ⬜ todo | |
| M9 — pooled-tenancy ADR | ⬜ todo | docs/ADR only |
| GA wrap — security + docs + green gate | ⬜ todo | |
