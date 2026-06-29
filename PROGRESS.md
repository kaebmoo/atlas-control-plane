# Atlas GA Completion — Progress Ledger

Tracks the run-to-completion stages in
[docs/plans/ga-completion-plan.md](docs/plans/ga-completion-plan.md). One line per
stage at close-out (gate green + docs synced + committed).

| Stage | Status | Notes |
|---|---|---|
| M3 — migrations + hardening | ✅ done | Versioned migration runner (`schema_version` + ordered idempotent steps) folding old `_migrate()`; `scripts/backup.sh`, `run-prod.sh`, example systemd unit; `ATLAS_REQUEST_LOG` JSON request log; secure defaults confirmed; `scripts/check_migrations.py` added to gate; ops docs added. |
| M6 — government pack | ⬜ todo | |
| M4 — Atlas Fleet | ⬜ todo | |
| M5+B3 — CDR export | ⬜ todo | |
| B2+B4 — usage view + alert | ⬜ todo | |
| M8 — pack signing | ⬜ todo | |
| B5 + M7/B7 — BYOK / inference readiness | ⬜ todo | |
| M9 — pooled-tenancy ADR | ⬜ todo | docs/ADR only |
| GA wrap — security + docs + green gate | ⬜ todo | |
