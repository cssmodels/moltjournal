#!/bin/sh
# Roll the VM back to the "Snapshot before MJ-2.1" commit.
# Order matters: the old code knows only migration 0001, so downgrade the
# database while the new code (which knows 0002) is still running.
set -eu
HOST="${MOLT_HOST:-ptornberg@145.38.198.168}"
cd "$(dirname "$0")/.."
SNAP="$(git rev-list -n 1 --grep='^Snapshot before MJ-2.1' HEAD)"
TMP="$(mktemp -d)"
git archive "$SNAP" | tar -x -C "$TMP"

ssh "$HOST" 'set -eu
cd /opt/moltjournal
sudo systemctl disable --now moltjournal-sweep.timer moltjournal-backup.timer || true
docker compose exec -T api alembic downgrade 0001'

rsync -az --exclude '.env' "$TMP/" "$HOST:/opt/moltjournal/"
# The MJ-2.1 review guard names columns the downgrade dropped; put the old one back.
ssh "$HOST" 'set -eu
cd /opt/moltjournal
rm -f migrations/versions/0002_mj_2_1.py
docker compose exec -T db psql -U molt -d moltjournal -q < sql/postgres_hardening.sql
docker compose up -d --build'
rm -rf "$TMP"
curl -s https://moltjournal.org/api/health; echo
