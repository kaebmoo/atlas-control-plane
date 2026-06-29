#!/usr/bin/env bash
# Canonical Atlas completion gate. Run from the repo root: scripts/gate.sh
# Requires Python 3.11+ (code uses datetime.UTC) and node (for the dashboard JS check).
# Every check is hermetic (own temp DB / ephemeral port / mock thClaws).
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m py_compile \
  atlas/config.py atlas/db.py atlas/app.py atlas/jobs.py atlas/workflows.py \
  atlas/router.py atlas/workflow_templates.py atlas/usage.py atlas/auth.py \
  atlas/admin.py atlas/packs.py atlas/byok.py \
  fleet/fleet.py fleet/cdr.py fleet/__main__.py fleet/check_fleet.py \
  scripts/check_workflows.py scripts/check_workflow_api.py scripts/check_usage.py \
  scripts/check_auth.py scripts/check_migrations.py scripts/check_packs.py \
  scripts/check_cdr.py scripts/check_byok_helper.py scripts/check_silo.py

node --check atlas/static/app.js

python3 scripts/check_workflow_db.py
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

echo "=== completion gate GREEN ==="
