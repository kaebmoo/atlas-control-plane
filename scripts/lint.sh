#!/usr/bin/env bash
# Dev-only static analysis. NOT part of scripts/gate.sh (which is hermetic + offline +
# stdlib-only). Run on demand / in CI to catch bug & security CLASSES deterministically,
# complementing the runtime hermetic checks. Tools are fetched ephemerally via uvx — nothing
# is added to the runtime dependency set (Atlas core stays stdlib-only).
#
# Usage: scripts/lint.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v uvx >/dev/null 2>&1; then
  echo "uvx not found — install uv (https://docs.astral.sh/uv/) to run the linters." >&2
  exit 127
fi

echo "=== ruff (pyflakes + bugbear: real bugs, dead code, unsafe patterns) ==="
# E402 is expected in scripts/ (sys.path shim before imports); focus on bug-class rules.
uvx ruff check atlas fleet --select F,B,E722

echo "=== bandit (security: injection, weak crypto, etc.) ==="
# B608 (f-string SQL) is guarded by atlas.db._set_clause (column names are [a-z_]-checked,
# values are parameterized); B105 fires on string CONSTANTS, not real passwords.
uvx bandit -q -r atlas fleet --severity-level medium --confidence-level medium \
  --skip B608,B105 || true

echo "=== static analysis OK (review any bandit output above) ==="
