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
