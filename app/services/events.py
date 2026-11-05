"""The append-only record of everything that happens."""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import constitution
from app.models import Event


def log(
    session: Session,
    kind: str,
    /,
    *,
    manuscript_id: str | None = None,
    operator_id: str | None = None,
    agent_id: str | None = None,
    subject_id: str | None = None,
    **payload,
) -> Event:
    event = Event(
        kind=kind,
        constitution_version=constitution.current().label,
        prompt_version=constitution.current().prompt_version,
        manuscript_id=manuscript_id,
        operator_id=operator_id,
        agent_id=agent_id,
        subject_id=subject_id,
        payload_json=json.dumps(payload, default=str, sort_keys=True),
    )
    session.add(event)
    session.flush()
    return event


def for_manuscript(session: Session, manuscript_id: str, kinds: tuple[str, ...] | None = None):
    query = select(Event).where(Event.manuscript_id == manuscript_id).order_by(Event.seq)
    if kinds:
        query = query.where(Event.kind.in_(kinds))
    return session.execute(query).scalars().all()


def public_payload(event: Event, pseudonymised: set[str]) -> dict:
    """The payload as published: a pseudonymised operator's name is never shown again."""
    payload = json.loads(event.payload_json or "{}")
    if event.operator_id in pseudonymised and "name" in payload:
        payload["name"] = "[pseudonymised]"
    return payload


def pseudonymised_operators(session: Session) -> set[str]:
    from app.models import Operator

    return set(session.execute(select(Operator.id).where(Operator.pseudonymised_at.is_not(None))).scalars().all())
