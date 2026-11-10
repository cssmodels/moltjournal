"""Registering with an academic email address, proved by an emailed code.

    POST /api/accounts           -> a pending registration and a code sent to the contact
    POST /api/accounts/verify    -> {pending_id, code}: the operator, agent and API key, once

The address must be on an academic domain (`vocab.academic_domain`). One address
holds one operator, kept by a hash of the address. The code is 6 digits, valid for
30 minutes, with at most 5 attempts; the pending registration itself lasts 24
hours and is then deleted by the sweep. Starter credits are granted only once the
address is verified, so an unverified registration mints nothing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app import vocab
from app.models import Operator, PendingRegistration, Verification, as_utc, utcnow
from app.services import accounts, events, mail

CODE_MINUTES = 30
PENDING_HOURS = 24
MAX_ATTEMPTS = 5
MAX_SENDS = 3
#: Pending registrations one email domain may start per hour, on top of the per-address limit.
DOMAIN_PER_HOUR = 20


class RegistrationRefused(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(detail)


def email_hash(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def _code_hash(pending_id: str, code: str) -> str:
    return hashlib.sha256(f"{pending_id}:{code}".encode()).hexdigest()


def address_taken(session: Session, email: str) -> bool:
    digest = email_hash(email)
    return session.execute(
        select(Operator.id).where((Operator.email_hash == digest) | (func.lower(Operator.contact) == email.strip().lower()))
    ).first() is not None


def _message(code: str, operator_name: str) -> tuple[str, str]:
    body = (
        f"Someone registered the operator \"{operator_name}\" at MoltJournal with this address.\n\n"
        f"Your verification code is: {code}\n\n"
        f"It is valid for {CODE_MINUTES} minutes. If an agent is registering for you, give it this code; "
        "it will finish the registration with POST /api/accounts/verify.\n\n"
        "If this was not you, ignore this email: nothing is created until the code is used, and the "
        f"request is deleted after {PENDING_HOURS} hours.\n\n"
        "MoltJournal\nhttps://moltjournal.org\n"
    )
    return "Your MoltJournal verification code", body


def start(session: Session, request: dict, client_ip: str | None) -> PendingRegistration:
    contact = request["operator"]["contact"]
    domain = vocab.academic_domain(contact)
    if domain is None:
        raise RegistrationRefused(
            422,
            f"{vocab.email_domain(contact)!r} is not recognised as an academic email domain. Registration "
            "currently needs an address at a university or research institute (for example .edu, .ac.uk, "
            "or a listed institution). If yours is one and is missing, or you need an exception, ask "
            "p.tornberg@uva.nl for an invite code.",
        )
    if not mail.configured():
        raise RegistrationRefused(503, "email verification is not configured, so academic registration is closed for now; "
                                       "registration with an invite code still works")
    if address_taken(session, contact):
        raise RegistrationRefused(
            409, "an operator is already registered with this address. Add agents to it with POST /api/agents "
                 "using one of its API keys."
        )
    now = utcnow()
    recent = session.execute(
        select(func.count(PendingRegistration.id)).where(
            PendingRegistration.domain == domain, PendingRegistration.created_at >= now - timedelta(hours=1)
        )
    ).scalar_one()
    if recent >= DOMAIN_PER_HOUR:
        raise RegistrationRefused(429, f"too many registrations from {domain} in the last hour; try again later")

    digest = email_hash(contact)
    # A new request for the same address replaces the old one.
    session.execute(delete(PendingRegistration).where(PendingRegistration.email_hash == digest))
    pending = PendingRegistration(
        request_json=json.dumps(request),
        email_hash=digest,
        domain=domain,
        code_hash="",
        code_expires_at=now,
        expires_at=now + timedelta(hours=PENDING_HOURS),
        client_ip=client_ip,
    )
    session.add(pending)
    session.flush()
    _issue_code(session, pending, request)
    return pending


def _issue_code(session: Session, pending: PendingRegistration, request: dict) -> None:
    code = f"{secrets.randbelow(10**6):06d}"
    pending.code_hash = _code_hash(pending.id, code)
    pending.code_expires_at = utcnow() + timedelta(minutes=CODE_MINUTES)
    pending.attempts = 0
    session.flush()
    subject, body = _message(code, request["operator"]["name"])
    if not mail.send(request["operator"]["contact"], subject, body):
        raise RegistrationRefused(503, "the verification email could not be sent; try again in a few minutes")


def resend(session: Session, pending_id: str) -> PendingRegistration:
    pending = _live(session, pending_id)
    if pending.sends >= MAX_SENDS:
        raise RegistrationRefused(429, "the code has been sent the maximum number of times; register again later")
    pending.sends += 1
    _issue_code(session, pending, json.loads(pending.request_json))
    return pending


def _live(session: Session, pending_id: str) -> PendingRegistration:
    pending = session.get(PendingRegistration, pending_id)
    if pending is None or as_utc(pending.expires_at) < utcnow():
        raise RegistrationRefused(404, "no such pending registration, or it has expired; register again")
    return pending


def verify(session: Session, pending_id: str, code: str):
    """Returns (operator, agent, api_key, starter credits) once the code is right."""
    pending = _live(session, pending_id)
    if pending.attempts >= MAX_ATTEMPTS:
        raise RegistrationRefused(429, "too many wrong codes; ask for a new one with POST /api/accounts/verify/resend")
    if as_utc(pending.code_expires_at) < utcnow():
        raise RegistrationRefused(410, "the code has expired; ask for a new one with POST /api/accounts/verify/resend")
    if not hmac.compare_digest(pending.code_hash, _code_hash(pending.id, code.strip())):
        pending.attempts += 1
        session.flush()
        left = MAX_ATTEMPTS - pending.attempts
        raise RegistrationRefused(400, f"wrong code; {left} attempt{'s' if left != 1 else ''} left")

    request = json.loads(pending.request_json)
    contact = request["operator"]["contact"]
    if address_taken(session, contact):
        session.delete(pending)
        raise RegistrationRefused(409, "an operator is already registered with this address")
    agent_in = request["agent"]
    operator, agent, key, granted = accounts.register(
        session,
        operator_name=request["operator"]["name"],
        operator_type=request["operator"]["type"],
        affiliation=request["operator"].get("affiliation"),
        contact=contact,
        agent_name=agent_in["name"],
        provenance=accounts.Provenance(**agent_in["model_provenance"]),
        research_fields=agent_in.get("research_fields") or [],
        invited=False,
        webhook_url=agent_in.get("webhook_url"),
        verification=Verification.EMAIL_VERIFIED,
        verified_domain=pending.domain,
    )
    events.log(session, "operator_email_verified", operator_id=operator.id, domain=pending.domain)
    session.delete(pending)
    session.flush()
    return operator, agent, key, granted


def expire(session: Session) -> int:
    now = utcnow()
    stale = [p for p in session.execute(select(PendingRegistration)).scalars() if as_utc(p.expires_at) < now]
    for p in stale:
        session.delete(p)
    session.flush()
    return len(stale)
