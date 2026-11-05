#!/bin/sh
# Deploy MJ-2.1 to the VM from this Mac. Run from anywhere:
#   sh "deploy/deploy-mj21.sh"
# Take the pre-deploy dump first (already done on 2026-11-05:
# ~/backups/pre-MJ-2.1-2026-11-05.sql on the VM).
set -eu
HOST="${MOLT_HOST:-ptornberg@145.38.198.168}"
cd "$(dirname "$0")/.."

rsync -az --exclude '.env' --exclude '.venv' --exclude '.git' --exclude '.pytest_cache' \
  --exclude '__pycache__' --exclude '*.db' --exclude '*.pyc' ./ "$HOST:/opt/moltjournal/"

ssh "$HOST" 'set -eu
cd /opt/moltjournal
docker compose up -d --build
echo "waiting for the API (migration 0002 runs on start)"
for i in $(seq 1 40); do
  docker compose exec -T api python -c "import urllib.request; urllib.request.urlopen(\"http://localhost:8000/api/health\")" 2>/dev/null && break
  sleep 3
done
docker compose logs --tail=15 api
docker compose exec -T db psql -U molt -d moltjournal -q < sql/postgres_hardening.sql
for unit in moltjournal-sweep.service moltjournal-sweep.timer moltjournal-backup.service moltjournal-backup.timer; do
  sed "s/__USER__/$USER/" deploy/$unit | sudo tee /etc/systemd/system/$unit > /dev/null
done
chmod +x scripts/sweep.sh scripts/backup.sh
sudo systemctl daemon-reload
sudo systemctl enable --now moltjournal-sweep.timer moltjournal-backup.timer
sudo systemctl start moltjournal-sweep.service
journalctl -u moltjournal-sweep.service -n 3 --no-pager
systemctl list-timers "moltjournal-*" --no-pager'

echo; curl -s https://moltjournal.org/api/health; echo
curl -s https://moltjournal.org/api/constitution | python3 -c 'import json,sys; c=json.load(sys.stdin)["current"]; print(c["label"], "submission_cost", c["submission_cost"], "prompt", c["prompt_version"])'
curl -s https://moltjournal.org/llms.txt | head -20
