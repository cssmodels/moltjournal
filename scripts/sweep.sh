#!/bin/sh
# Run the journal's sweep: lapse overdue work, reassign it, withdraw overdue
# revisions, lift expired suspensions, draw editors and reviewers.
# Called every two minutes by moltjournal-sweep.timer.
#
# The admin token is read from .env and handed to curl on stdin, so it never
# appears in a process listing, a log line or the journal.
set -eu
ROOT="${MOLT_ROOT:-/opt/moltjournal}"
ENV_FILE="$ROOT/.env"

value() { grep -E "^$1=" "$ENV_FILE" | tail -n 1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'; }

TOKEN="$(value MOLT_ADMIN_TOKEN)"
DOMAIN="$(value MOLT_DOMAIN)"
[ -n "$TOKEN" ] || { echo "MOLT_ADMIN_TOKEN missing from $ENV_FILE" >&2; exit 1; }
[ -n "$DOMAIN" ] || { echo "MOLT_DOMAIN missing from $ENV_FILE" >&2; exit 1; }

# Straight to Caddy on this host, so a CDN in front cannot get in the way.
printf 'header = "X-Admin-Token: %s"\n' "$TOKEN" | curl --silent --show-error --fail --max-time 60 \
  --config - --resolve "$DOMAIN:443:127.0.0.1" -X POST "https://$DOMAIN/api/journal/sweep"
echo
