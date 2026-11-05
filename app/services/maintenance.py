"""Housekeeping. Idempotent, so it is safe to run as often as you like.

Nothing here makes a scientific judgement. It only keeps the pipeline moving:
expiring review assignments nobody answered, and drawing editors and reviewers
for work that was waiting for somebody eligible to exist.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import constitution
from app.models import (
    AssignmentState,
    Manuscript,
    Review,
    ReviewAssignment,
    State,
    as_utc,
    utcnow,
)
from app.services import assignment, events, manuscripts, reviews


def sweep(session: Session) -> dict:
    summary = {"expired": 0, "editors_assigned": 0, "reviewers_assigned": 0, "evaluators_assigned": 0}
    now = utcnow()

    overdue = session.execute(
        select(ReviewAssignment).where(ReviewAssignment.state == AssignmentState.OFFERED)
    ).scalars().all()
    for row in overdue:
        if as_utc(row.due_at) < now:
            row.state = AssignmentState.EXPIRED
            row.responded_at = now
            summary["expired"] += 1
            events.log(
                session,
                "assignment_expired",
                manuscript_id=row.manuscript_id,
                operator_id=row.reviewer_operator_id,
                subject_id=row.id,
            )

    open_manuscripts = session.execute(
        select(Manuscript).where(Manuscript.state.not_in(State.TERMINAL))
    ).scalars().all()
    for manuscript in open_manuscripts:
        if manuscript.state == State.SUBMITTED:
            if manuscripts.start_review(session, manuscript):
                summary["editors_assigned"] += 1
            continue
        if assignment.current_editor(session, manuscript) is None:
            if assignment.assign_editor(session, manuscript):
                summary["editors_assigned"] += 1
        if manuscript.state == State.REVIEWERS_ASSIGNED:
            summary["reviewers_assigned"] += len(assignment.fill_panel(session, manuscript))
            reviews.refresh_panel(session, manuscript)

    waiting = session.execute(
        select(Review).where(Review.evaluator_agent_id.is_(None), Review.kind == "post")
    ).scalars().all()
    for review in waiting:
        if reviews.assign_evaluator(session, review):
            summary["evaluators_assigned"] += 1

    session.flush()
    return summary
