"""Housekeeping. Idempotent, so it is safe to run as often as you like, and it
runs on a timer every couple of minutes.

Nothing here makes a scientific judgement. It only enforces the clock and keeps
the pipeline moving: work past its deadline is taken away and reassigned (a
lapse for whoever held it), revisions past theirs are withdrawn, automatic
suspensions that have run out are lifted, and editors, reviewers, evaluators and
appeal editors are drawn for work that was waiting for somebody eligible.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Appeal,
    AppealState,
    AssignmentState,
    EditorAssignment,
    EditorAssignmentState,
    Manuscript,
    Review,
    ReviewAssignment,
    ReviewKind,
    State,
    utcnow,
)
from app.services import (
    appeals,
    assignment,
    deadlines,
    events,
    lifecycle,
    manuscripts,
    reviews,
    suspension,
)


def sweep(session: Session) -> dict:
    summary = {
        "pending_registrations_expired": 0,
        "suspensions_lifted": 0,
        "expired": 0,
        "editors_lapsed": 0,
        "evaluators_lapsed": 0,
        "appeal_editors_lapsed": 0,
        "revisions_withdrawn": 0,
        "editors_assigned": 0,
        "reviewers_assigned": 0,
        "evaluators_assigned": 0,
        "appeal_editors_assigned": 0,
    }
    now = utcnow()
    summary["suspensions_lifted"] = suspension.lift_expired(session)
    from app.services import registration

    summary["pending_registrations_expired"] = registration.expire(session)

    # Reviews not filed in time.
    for row in session.execute(
        select(ReviewAssignment).where(ReviewAssignment.state == AssignmentState.OFFERED)
    ).scalars().all():
        if deadlines.overdue(row.due_at, now):
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
            deadlines.record_lapse(
                session, row.reviewer_operator_id, "reviewer", manuscript_id=row.manuscript_id, subject_id=row.id
            )

    # Editors who owed marks or a decision and did not deliver.
    for row in session.execute(
        select(EditorAssignment).where(
            EditorAssignment.state == EditorAssignmentState.ASSIGNED, EditorAssignment.due_at.is_not(None)
        )
    ).scalars().all():
        manuscript = session.get(Manuscript, row.manuscript_id)
        if manuscript.state in State.TERMINAL or not deadlines.overdue(row.due_at, now):
            continue
        row.state = EditorAssignmentState.LAPSED
        row.due_at = None
        session.flush()
        summary["editors_lapsed"] += 1
        deadlines.record_lapse(
            session, row.editor_operator_id, "editor", manuscript_id=manuscript.id, subject_id=row.id
        )
        if assignment.assign_editor(session, manuscript) is not None:
            summary["editors_assigned"] += 1

    # Post-publication reviews nobody evaluated in time.
    for review in session.execute(
        select(Review).where(
            Review.kind == ReviewKind.POST, Review.evaluation.is_(None), Review.evaluator_operator_id.is_not(None)
        )
    ).scalars().all():
        if deadlines.overdue(review.evaluator_due_at, now):
            gone = review.evaluator_operator_id
            review.evaluator_operator_id = None
            review.evaluator_agent_id = None
            review.evaluator_due_at = None
            session.flush()
            summary["evaluators_lapsed"] += 1
            deadlines.record_lapse(session, gone, "evaluator", manuscript_id=review.manuscript_id, subject_id=review.id)
            reviews.assign_evaluator(session, review, exclude={gone})

    # Appeals not ruled on in time.
    for appeal in session.execute(
        select(Appeal).where(Appeal.state == AppealState.PENDING, Appeal.appeal_editor_operator_id.is_not(None))
    ).scalars().all():
        if deadlines.overdue(appeal.due_at, now):
            gone = appeal.appeal_editor_operator_id
            appeals.release_editor(session, appeal, reason="deadline missed")
            summary["appeal_editors_lapsed"] += 1
            deadlines.record_lapse(
                session, gone, "appeal_editor", manuscript_id=appeal.manuscript_id, subject_id=appeal.id
            )

    # Revisions not delivered in time are withdrawn.
    for manuscript in session.execute(
        select(Manuscript).where(Manuscript.state == State.REVISION)
    ).scalars().all():
        if deadlines.overdue(manuscript.revision_due_at, now):
            manuscript.revision_due_at = None
            lifecycle.transition(
                session, manuscript, State.WITHDRAWN, operator_id=manuscript.operator_id,
                note="revision deadline passed; withdrawn automatically",
            )
            events.log(
                session, "revision_deadline_missed", manuscript_id=manuscript.id, operator_id=manuscript.operator_id
            )
            summary["revisions_withdrawn"] += 1

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
        select(Review).where(
            Review.evaluator_agent_id.is_(None), Review.kind == ReviewKind.POST, Review.evaluation.is_(None)
        )
    ).scalars().all()
    for review in waiting:
        if reviews.assign_evaluator(session, review):
            summary["evaluators_assigned"] += 1

    for appeal in session.execute(
        select(Appeal).where(Appeal.state == AppealState.PENDING, Appeal.appeal_editor_operator_id.is_(None))
    ).scalars().all():
        if appeals.assign_appeal_editor(session, appeal):
            summary["appeal_editors_assigned"] += 1

    session.flush()
    return summary
