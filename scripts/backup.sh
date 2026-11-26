#!/bin/bash
# Copy the data folder (the database and any queued files, all encrypted) to a dated archive.
#   ./scripts/backup.sh                 into ./backups
#   ./scripts/backup.sh /Volumes/NAS/ocr
# The archive is useless without OCR_STORAGE_KEY from .env. Keep a copy of that key somewhere else
# (a password manager), never beside the backups. Archives older than 30 days in the folder are removed.
set -euo pipefail
cd "$(dirname "$0")/.."
DATA="$(grep -E '^OCR_DATA_DIR=' .env | tail -1 | cut -d= -f2-)"; DATA="${DATA:-./data}"
DEST="${1:-./backups}"
[ -d "$DATA" ] || { echo "No data folder at $DATA"; exit 1; }
mkdir -p "$DEST"
OUT="$DEST/get-ocr-$(date +%Y%m%d-%H%M%S).tar.gz"
# the database is copied through sqlite so a write in progress cannot spoil the copy
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/data"
PY=.venv/bin/python; [ -x "$PY" ] || PY=python3
for db in "$DATA"/*.db; do
  [ -f "$db" ] || continue
  "$PY" -c 'import sqlite3, sys; a = sqlite3.connect(sys.argv[1]); b = sqlite3.connect(sys.argv[2]); a.backup(b); b.close(); a.close()' "$db" "$TMP/data/$(basename "$db")"
done
[ -d "$DATA/inputs" ] && cp -R "$DATA/inputs" "$TMP/data/inputs"
tar -czf "$OUT" -C "$TMP" data
date -u +%Y-%m-%dT%H:%M:%SZ > "$DATA/last_backup"   # the service shows this in /health and warns when it grows old
find "$DEST" -name 'get-ocr-*.tar.gz' -mtime +30 -delete
echo "✓ $OUT ($(du -h "$OUT" | cut -f1))"
