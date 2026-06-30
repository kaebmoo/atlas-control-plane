#!/usr/bin/env bash
# Static analysis — FAIL-CLOSED. A new bug/security-class finding makes this exit non-zero.
# Runs as a required CI job (see .github/workflows/ci.yml), SEPARATE from the hermetic offline
# gate (scripts/gate.sh). Tools are fetched ephemerally via uvx and PINNED, so runtime stays
# stdlib-only and results are reproducible. Suppressions are per-line `# nosec <code>` with a
# reason in the source — there is NO global skip and NO `|| true`, so nothing is silently hidden.
#
# Usage: scripts/lint.sh
set -euo pipefail
cd "$(dirname "$0")/.."

RUFF="ruff@0.15.20"
BANDIT="bandit@1.9.4"

if ! command -v uvx >/dev/null 2>&1; then
  echo "uvx required (https://docs.astral.sh/uv/) — install uv to run the linters." >&2
  exit 127
fi

echo "=== ruff (pyflakes + bugbear: real bugs, dead code, unsafe patterns) ==="
uvx "$RUFF" check atlas fleet --select F,B,E722

echo "=== bandit (security, medium+ severity; reviewed B608/B310 carry per-line # nosec) ==="
# Capture so we can drop bandit's cosmetic "nosec encountered, but no failed test" chatter
# (one per suppressed line) WITHOUT touching the real exit code — still fail-closed on findings.
set +e
bandit_out="$(uvx "$BANDIT" -q -r atlas fleet --severity-level medium 2>&1)"
bandit_rc=$?
set -e
printf '%s\n' "$bandit_out" | grep -v "nosec encountered" || true
[ "$bandit_rc" -eq 0 ] || { echo "bandit found unsuppressed issues (see above)" >&2; exit 1; }

echo "lint OK"
