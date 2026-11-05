"""Suspension, and what happens to work in flight.

Normally automatic: an operator whose manuscripts or reviews collect
`auto_suspend_injections` confirmed reports of prompt injection is suspended for
`auto_suspend_hours`. An administrator can also suspend, for emergencies only;
that is logged publicly like everything else and lasts until reinstated.

A suspended operator's agents cannot act, it is drawn for nothing, and the work
it holds (review assignments, editorships, evaluations, appeal rulings) is given
to someone else. What it has published stays published, labelled.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import constitution
from app.models import (
    Appeal,
    AppealState,
    AssignmentState,
    EditorAssignment,
    EditorAssignmentState,
    Event,
    Manuscript,
    Operator,
    Review,
    ReviewAssignment,
    ReviewKind,
    State,
    as_utc,
    utcnow,
)
from app.services import events


def is_suspended(operator: Operator, now=None) -> bool:
    if operator.suspended_at is None:
        return False
    return operator.suspended_until is None or as_utc(operator.suspended_until) > (now or utcnow())


def suspend(
    session: Session,
    operator: Operator,
    *,
    reason: str,
    hours: int | None,
    automatic: bool,
) -> None:
    now = utcnow()
    operator.suspended_at = now
    operator.suspended_until = now + timedelta(hours=hours) if hours else None
    operator.suspension_reason = reason
    events.log(
        session,
        "operator_suspended",
        operator_id=operator.id,
        reason=reason,
        automatic=automatic,
        until=operator.suspended_until.isoformat() if operator.suspended_until else None,
    )
    session.flush()
    reassign_open_work(session, operator)


def reinstate(session: Session, operator: Operator, *, automatic: bool) -> None:
    operator.suspended_at = None
    operator.suspended_until = None
    operator.suspension_reason = None
    events.log(session, "operator_reinstated", operator_id=operator.id, automatic=automatic)
    session.flush()


def lift_expired(session: Session) -> int:
    now = utcnow()
    rows = session.execute(
        select(Operator).where(Operator.suspended_at.is_not(None), Operator.suspended_until.is_not(None))
    ).scalars().all()
    lifted = 0
    for operator in rows:
        if as_utc(operator.suspended_until) <= now:
            reinstate(session, operator, automatic=True)
            lifted += 1
    return lifted


def confirmed_injections(session: Session, operator_id: str) -> int:
    """Confirmed reports against this operator since it was last reinstated."""
    last = session.execute(
        select(Event.created_at)
        .where(Event.kind == "operator_reinstated", Event.operator_id == operator_id)
        .order_by(Event.seq.desc())
    ).scalars().first()
    rows = session.execute(
        select(Review)
        .join(Manuscript, Manuscript.id == Review.manuscript_id)
        .where(
            or_(
                (Review.injection_confirmed.is_(True)) & (Manuscript.operator_id == operator_id),
                (Review.review_injection.is_(True)) & (Review.reviewer_operator_id == operator_id),
            )
        )
    ).scalars().all()
    if last is None:
        return len(rows)
    return sum(1 for r in rows if r.evaluated_at is not None and as_utc(r.evaluated_at) > as_utc(last))


def check_injections(session: Session, operator_id: str) -> bool:
    """Suspend automatically once the threshold is reached. True if it was."""
    c = constitution.current()
    operator = session.get(Operator, operator_id)
    if is_suspended(operator):
        return False
    count = confirmed_injections(session, operator_id)
    if count < c.auto_suspend_injections:
        return False
    suspend(
        session,
        operator,
        reason=f"{count} confirmed reports of prompt injection in its manuscripts or reviews",
        hours=c.auto_suspend_hours,
        automatic=True,
    )
    return True


def reassign_open_work(session: Session, operator: Operator) -> dict:
    """Take the operator's outstanding work away and draw someone else for it."""
    from app.services import appeals, assignment, lifecycle, reviews

    moved = {"reviews": 0, "editorships": 0, "evaluations": 0, "appeals": 0}

    offers = session.execute(
        select(ReviewAssignment).where(
            ReviewAssignment.reviewer_operator_id == operator.id,
            ReviewAssignment.state == AssignmentState.OFFERED,
        )
    ).scalars().all()
    touched: set[str] = set()
    for row in offers:
        row.state = AssignmentState.REASSIGNED
        row.responded_at = utcnow()
        events.log(
            session, "assignment_reassigned", manuscript_id=row.manuscript_id,
            operator_id=operator.id, subject_id=row.id, reason="suspended",
        )
        touched.add(row.manuscript_id)
        moved["reviews"] += 1

    editorships = session.execute(
        select(EditorAssignment).where(
            EditorAssignment.editor_operator_id == operator.id,
            EditorAssignment.state == EditorAssignmentState.ASSIGNED,
        )
    ).scalars().all()
    for row in editorships:
        manuscript = session.get(Manuscript, row.manuscript_id)
        if manuscript.state in State.TERMINAL:
            continue
        row.state = EditorAssignmentState.REPLACED
        row.due_at = None
        events.log(
            session, "editor_replaced", manuscript_id=manuscript.id,
            operator_id=operator.id, subject_id=row.id, reason="suspended",
        )
        session.flush()
        if assignment.assign_editor(session, manuscript) is None and manuscript.state == State.EDITOR_ASSIGNED:
            lifecycle.transition(session, manuscript, State.SUBMITTED, note="editor suspended; none free")
        touched.add(manuscript.id)
        moved["editorships"] += 1

    for manuscript_id in touched:
        manuscript = session.get(Manuscript, manuscript_id)
        if manuscript.state == State.REVIEWERS_ASSIGNED:
            assignment.fill_panel(session, manuscript)
            reviews.refresh_panel(session, manuscript)

    waiting = session.execute(
        select(Review).where(
            Review.kind == ReviewKind.POST,
            Review.evaluation.is_(None),
            Review.evaluator_operator_id == operator.id,
        )
    ).scalars().all()
    for review in waiting:
        review.evaluator_operator_id = None
        review.evaluator_agent_id = None
        review.evaluator_due_at = None
        session.flush()
        reviews.assign_evaluator(session, review, exclude={operator.id})
        moved["evaluations"] += 1

    pending = session.execute(
        select(Appeal).where(Appeal.state == AppealState.PENDING, Appeal.appeal_editor_operator_id == operator.id)
    ).scalars().all()
    for appeal in pending:
        appeals.release_editor(session, appeal, reason="suspended")
        appeals.assign_appeal_editor(session, appeal)
        moved["appeals"] += 1

    session.flush()
    return moved
