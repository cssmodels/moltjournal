"""Friendly public identifiers. UUIDs inside, MOLT-ids outside."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import IdSequence


def next_serial(session: Session, name: str) -> int:
    row = session.get(IdSequence, name, with_for_update=True)
    if row is None:
        row = IdSequence(name=name, value=0)
        session.add(row)
        session.flush()
        row = session.get(IdSequence, name, with_for_update=True)
    row.value += 1
    session.flush()
    return row.value


def operator_id(session: Session) -> str:
    return f"MOLT-O-{next_serial(session, 'operator'):06d}"


def agent_id(session: Session) -> str:
    return f"MOLT-A-{next_serial(session, 'agent'):08d}"


def manuscript_id(session: Session, year: int) -> str:
    return f"MOLT-{year}-{next_serial(session, f'manuscript-{year}'):06d}"


def review_assignment_id(session: Session) -> str:
    return f"MOLT-RA-{next_serial(session, 'review-assignment'):08d}"


def editor_assignment_id(session: Session) -> str:
    return f"MOLT-EA-{next_serial(session, 'editor-assignment'):07d}"


def review_id(session: Session) -> str:
    return f"MOLT-R-{next_serial(session, 'review'):08d}"


def decision_id(session: Session) -> str:
    return f"MOLT-D-{next_serial(session, 'decision'):07d}"
