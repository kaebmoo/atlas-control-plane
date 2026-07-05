#!/usr/bin/env bash
# Canonical Atlas completion gate. Run from the repo root: scripts/gate.sh
# Requires Python 3.11+ (code uses datetime.UTC) and node (for the dashboard JS check).
# Every check is hermetic (own temp DB / ephemeral port / mock thClaws).
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m py_compile \
  atlas/config.py atlas/db.py atlas/app.py atlas/jobs.py atlas/workflows.py \
  atlas/router.py atlas/workflow_templates.py atlas/usage.py atlas/auth.py \
  atlas/admin.py atlas/packs.py atlas/byok.py atlas/outbound.py \
  fleet/fleet.py fleet/cdr.py fleet/__main__.py fleet/check_fleet.py \
  scripts/check_workflows.py scripts/check_workflow_api.py scripts/check_usage.py \
  scripts/check_auth.py scripts/check_migrations.py scripts/check_packs.py \
  scripts/check_cdr.py scripts/check_byok_helper.py scripts/check_silo.py \
  scripts/check_jobs.py scripts/check_backup.py scripts/check_audit_fixes.py \
  scripts/check_docs.py scripts/check_stress.py scripts/check_fuzz.py \
  scripts/check_ui_ux.py scripts/check_input_adapter.py scripts/check_outbound.py \
  scripts/check_observability.py scripts/check_permit_poc.py scripts/check_event_views.py \
  scripts/check_lib.py scripts/check_async_jobs.py scripts/check_worker_state.py \
  scripts/check_file_collection.py scripts/check_file_handoff.py scripts/check_dashboard_surfaces.py

node --check atlas/static/app.js

python3 scripts/check_workflow_db.py
python3 scripts/check_jobs.py
python3 scripts/check_workflows.py
python3 scripts/check_workflow_api.py
python3 scripts/check_auth.py
python3 scripts/check_usage.py
python3 scripts/check_migrations.py   # M3
python3 scripts/check_packs.py        # M6 + M8 signing
python3 fleet/check_fleet.py          # M4
python3 scripts/check_cdr.py          # M5 + B3
python3 scripts/check_byok_helper.py  # B5
python3 scripts/check_silo.py         # M9 silo invariant
python3 scripts/check_backup.py       # backup includes the upload store
python3 scripts/check_audit_fixes.py  # terminal-state races, run snapshot, trigger/limit guards
python3 scripts/check_docs.py         # docs-drift: README links + route coverage
python3 scripts/check_ui_ux.py        # dashboard UX: job sync, mobile drawer, modal focus
python3 scripts/check_event_views.py  # T2: tool/skill timeline builder, name escaping, dispatch
python3 scripts/check_worker_state.py # T4: sync_mode gate, busy probe, advisory router tie-break
python3 scripts/check_stress.py       # concurrency: atomic transitions converge under load
python3 scripts/check_fuzz.py         # parsers never crash; validators raise only ValueError
python3 scripts/check_input_adapter.py  # IA-1: envelope + provenance audit
python3 scripts/check_outbound.py       # OB-1: signed outbound delivery + deliveries API
python3 scripts/check_observability.py  # cross-cutting: metrics, audit export, classification + purge
python3 scripts/check_permit_poc.py     # permit PoC: operator dashboard escapes untrusted fields
python3 scripts/check_async_jobs.py     # T3: x_callback async jobs — pre-auth carve-out, HMAC token, reaper, races
python3 scripts/check_file_collection.py # T5: sync/export collection barrier — safe tar, caps, 409 retry, ordering
python3 scripts/check_file_handoff.py   # T6: sync/push file handoff — two workers, policy gate, additive, push failure
python3 scripts/check_dashboard_surfaces.py  # T1/T3/T5/T6 web surfaces: usage tokens+cost, job collect/async, builder handoff

echo "=== completion gate GREEN ==="
