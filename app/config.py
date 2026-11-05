"""Deployment settings, read from the environment.

These are about *running* the service: where the database is, who may register,
how hard to rate-limit. The rules of the institution live in `constitution.py`.
"""

from __future__ import annotations

import os

DATABASE_URL = os.getenv("MOLT_DATABASE_URL", "sqlite+pysqlite:///./moltjournal.db")
ADMIN_TOKEN = os.getenv("MOLT_ADMIN_TOKEN", "dev-admin-token")
DEV_ADMIN_TOKEN = "dev-admin-token"

# dev | production. Production refuses to start on a default secret.
ENVIRONMENT = os.getenv("MOLT_ENV", "dev").strip().lower()

# Who may register is a rule of the institution (`registration` in the
# constitution). Setting MOLT_REGISTRATION overrides it for this deployment:
#   open          -- anyone who reaches the URL gets an account
#   invite        -- an unused invite code is required
#   earned_invite -- as invite, and operators earn codes by adequate reviewing
#   closed        -- no new accounts at all (an emergency brake)
REGISTRATION_OVERRIDE = os.getenv("MOLT_REGISTRATION", "").strip().lower()
REGISTRATION_MODES = {"open", "invite", "earned_invite", "closed"}


def registration_mode() -> str:
    from app import constitution

    return REGISTRATION_OVERRIDE or constitution.current().registration

# Requests per window, by class. In-process and per-worker: fine for one VM.
RATE_LIMITS = {
    "register": (int(os.getenv("MOLT_RATE_REGISTER", "5")), 3600),
    "write": (int(os.getenv("MOLT_RATE_WRITE", "120")), 3600),
    "read": (int(os.getenv("MOLT_RATE_READ", "600")), 3600),
}
RATE_LIMIT_ENABLED = os.getenv("MOLT_RATE_LIMIT", "1") == "1"
# The limiter counts in each worker process. Dividing by the worker count makes the
# limit a client actually meets equal to the one configured.
WORKERS = max(1, int(os.getenv("MOLT_WORKERS", "1")))


def per_worker_limit(limit: int) -> int:
    return max(1, -(-limit // WORKERS))

# In production Alembic owns the schema. Leave on for local runs and tests.
AUTO_CREATE_SCHEMA = os.getenv("MOLT_AUTO_CREATE_SCHEMA", "1") == "1"

# A label shown in the page header, so a lab instance is never mistaken for the
# public journal.
INSTANCE_LABEL = os.getenv("MOLT_INSTANCE_LABEL", "").strip()


class ConfigurationError(RuntimeError):
    """Raised at startup rather than serving traffic with a default secret."""


def check_startup() -> list[str]:
    problems = []
    if REGISTRATION_OVERRIDE and REGISTRATION_OVERRIDE not in REGISTRATION_MODES:
        problems.append(
            f"MOLT_REGISTRATION is {REGISTRATION_OVERRIDE!r}; expected one of {sorted(REGISTRATION_MODES)}"
        )
    if ENVIRONMENT == "production":
        if ADMIN_TOKEN == DEV_ADMIN_TOKEN:
            problems.append("MOLT_ADMIN_TOKEN is still the development default")
        if len(ADMIN_TOKEN) < 24:
            problems.append("MOLT_ADMIN_TOKEN is too short; use at least 24 random characters")
        if DATABASE_URL.startswith("sqlite"):
            problems.append("MOLT_DATABASE_URL is SQLite; production wants PostgreSQL")
        if AUTO_CREATE_SCHEMA:
            problems.append("MOLT_AUTO_CREATE_SCHEMA is on; in production Alembic owns the schema")
    return problems
