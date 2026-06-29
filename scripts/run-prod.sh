#!/usr/bin/env bash
# Production launcher with secure defaults: auth required, no loopback bypass.
# Real secrets belong in the environment / systemd EnvironmentFile, never here.
set -euo pipefail
cd "$(dirname "$0")/.."

: "${ATLAS_SECRET_KEY:?set ATLAS_SECRET_KEY (HMAC for token/usage/pack signing)}"

# Never bypass auth in production. Forced off unconditionally: behind a reverse
# proxy all traffic looks like loopback, so a leaked ATLAS_LOOPBACK_NO_AUTH=true
# would make every proxied request admin-authenticated without a token.
export ATLAS_LOOPBACK_NO_AUTH=false
# Structured request logs on by default (override with ATLAS_REQUEST_LOG=false).
export ATLAS_REQUEST_LOG="${ATLAS_REQUEST_LOG:-true}"

# Bind to loopback; a reverse proxy terminates TLS in front (docs/ops/deployment.md).
# Set ATLAS_HOST=0.0.0.0 only when fronted by a proxy/firewall.
exec python3 -m atlas --host "${ATLAS_HOST:-127.0.0.1}" --port "${ATLAS_PORT:-8787}"
