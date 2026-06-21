#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
exec python3 -m atlas --host "${ATLAS_HOST:-127.0.0.1}" --port "${ATLAS_PORT:-8787}"
