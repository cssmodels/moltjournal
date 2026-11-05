#!/bin/sh
# Nightly database dump to ~/backups, keeping 14 days. Run by moltjournal-backup.timer.
set -eu
ROOT="${MOLT_ROOT:-/opt/moltjournal}"
DEST="${MOLT_BACKUP_DIR:-$HOME/backups}"
mkdir -p "$DEST"
chmod 700 "$DEST"
OUT="$DEST/molt-$(date +%F).sql.gz"
docker compose --project-directory "$ROOT" -f "$ROOT/docker-compose.yml" exec -T db \
  pg_dump -U molt moltjournal | gzip > "$OUT.partial"
mv "$OUT.partial" "$OUT"
chmod 600 "$OUT"
find "$DEST" -maxdepth 1 -name 'molt-*.sql.gz' -mtime +13 -delete
echo "wrote $OUT ($(du -h "$OUT" | cut -f1))"
