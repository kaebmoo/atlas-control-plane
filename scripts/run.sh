#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi

exec python3 -m atlas --host "${ATLAS_HOST:-127.0.0.1}" --port "${ATLAS_PORT:-8787}"
