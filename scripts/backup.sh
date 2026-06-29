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

# file_ref artifact bytes live outside SQLite under the upload dir; back them up too, AFTER
# the DB snapshot so every artifact record in the snapshot has its file present on restore.
UPLOAD_DIR="${ATLAS_UPLOAD_DIR:-$(dirname "$DB")/uploads}"
if [[ -d "$UPLOAD_DIR" ]]; then
  UPLOAD_DEST="$DEST_DIR/atlas-uploads-$STAMP.tar.gz"
  tar -czf "$UPLOAD_DEST" -C "$(dirname "$UPLOAD_DIR")" "$(basename "$UPLOAD_DIR")"
  echo "uploads written: $UPLOAD_DEST"
fi
