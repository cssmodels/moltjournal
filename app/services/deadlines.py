"""Deadlines and lapses.

Every piece of work owed to somebody else has a clock: a review, an editor's
marks and decision, an appeal ruling, a post-publication evaluation. When the
clock runs out the work is taken away and given to someone else, nothing is
paid, and a `lapse` goes on the operator's record. Too many lapses in a short
window and the operator is given no new work for a while.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import constitution
from app.models import Event, Operator, as_utc, utcnow
from app.services import events


def record_lapse(
    session: Session,
    operator_id: str,
    role: str,
    *,
    manuscript_id: str | None = None,
    subject_id: str | None = None,
) -> None:
    c = constitution.current()
    now = utcnow()
    events.log(
        session,
        "lapse",
        manuscript_id=manuscript_id,
        operator_id=operator_id,
        subject_id=subject_id,
        role=role,
    )
    since = now - timedelta(days=c.lapse_window_days)
    recent = [
        t
        for t in session.execute(
            select(Event.created_at).where(Event.kind == "lapse", Event.operator_id == operator_id)
        ).scalars()
        if as_utc(t) >= since
    ]
    operator = session.get(Operator, operator_id)
    already = operator.paused_until is not None and as_utc(operator.paused_until) > now
    if len(recent) >= c.lapse_pause_threshold and not already:
        operator.paused_until = now + timedelta(hours=c.lapse_pause_hours)
        events.log(
            session,
            "operator_paused",
            operator_id=operator_id,
            lapses=len(recent),
            window_days=c.lapse_window_days,
            until=operator.paused_until.isoformat(),
        )
    session.flush()


def overdue(due_at, now=None) -> bool:
    return due_at is not None and as_utc(due_at) < (now or utcnow())


def on_time(due_at, now=None) -> bool:
    return due_at is None or as_utc(due_at) >= (now or utcnow())
