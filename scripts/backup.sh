#!/usr/bin/env bash
# Online SQLite backup of the Atlas database (WAL-safe; no need to stop Atlas).
# Usage: scripts/backup.sh [dest-dir]
#   DB source:  $ATLAS_DB        (default: ./data/atlas.sqlite)
#   dest dir:   $1 or ./backups
set -euo pipefail
cd "$(dirname "$0")/.."

DB="${ATLAS_DB:-$PWD/data/atlas.sqlite}"
DEST_DIR="${1:-$PWD/backups}"

if [[ ! -f "$DB" ]]; then
  echo "atlas db not found: $DB" >&2
  exit 1
fi

mkdir -p "$DEST_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="$DEST_DIR/atlas-$STAMP.sqlite"

# sqlite3 .backup takes a consistent snapshot including committed WAL pages while
# the server keeps running (single writer). Restore: see docs/ops/backup-restore.md.
sqlite3 "$DB" ".backup '$DEST'"
echo "backup written: $DEST"
