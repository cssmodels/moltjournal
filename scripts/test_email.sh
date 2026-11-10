#!/bin/sh
# Send one test email with the SMTP settings in /opt/moltjournal/.env, from inside the API
# container, and say whether it worked. Prints no credentials.
#
#   scripts/test_email.sh you@your-university.edu
set -eu
[ $# -eq 1 ] || { echo "usage: $0 address" >&2; exit 2; }
ROOT="${MOLT_ROOT:-/opt/moltjournal}"
docker compose --project-directory "$ROOT" -f "$ROOT/docker-compose.yml" exec -T api python - "$1" <<'PY'
import sys
from email.message import EmailMessage

from app.services import mail

if not mail.configured():
    sys.exit("SMTP is not configured: set SMTP_HOST and SMTP_FROM in .env, then: docker compose up -d api")
message = EmailMessage()
message["From"] = mail._env("SMTP_FROM")
message["To"] = sys.argv[1]
message["Subject"] = "MoltJournal test email"
message.set_content("This is a test of MoltJournal's verification email. If you can read it, sending works.\n\nMoltJournal\n")
try:
    mail._send(message)
except Exception as exc:  # report the server's answer, which never contains the password
    sys.exit(f"sending failed: {type(exc).__name__}: {exc}")
print(f"sent to {sys.argv[1]} from {message['From']} via {mail._env('SMTP_HOST')}:{mail._env('SMTP_PORT') or 587}")
PY
