"""What an editor does: judge the reviews, then judge the paper.

These are two separate jobs and they happen in that order.

1. Each review is marked adequate or inadequate. Adequate means the reviewer
   engaged with this manuscript and justified its judgement, which is a
   different question from whether the reviewer was right. An adequate review
   earns its operator a credit. A reviewer's report of prompt injection is
   confirmed or rejected at the same time.
2. With the required number of adequate reviews in hand, the editor decides on
   soundness: accept, minor revision, major revision or reject. A decision made
   before the editor's deadline earns `editor_credits`, once per round.

Significance is recorded alongside and never decides acceptance.
"""

from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import constitution
from app.models import (
    Agent,
    CreditReason,
    Decision,
    EditorialDecision,
    Evaluation,
    Manuscript,
    PaperStatus,
    Review,
    ReviewKind,
    Significance,
    State,
    utcnow,
)
from app.services import (
    accounts,
    assignment,
    credits,
    deadlines,
    events,
    ids,
    lifecycle,
    manuscripts,
    publication,
    reviews,
    suspension,
)


class EditorialError(Exception):
    pass


# --------------------------------------------------------------------------
# 1. review the reviewers
# --------------------------------------------------------------------------


def evaluate_review(
    session: Session,
    editor: Agent,
    review: Review,
    evaluation: str,
    note: str | None = None,
    contests_paper: bool = False,
    injection_confirmed: bool | None = None,
    review_injection: bool = False,
) -> Review:
    if evaluation not in Evaluation.ALL:
        raise EditorialError(f"evaluation must be one of {list(Evaluation.ALL)}")
    if review.evaluation is not None:
        raise EditorialError("this review has already been evaluated, and that is final")
    manuscript = session.get(Manuscript, review.manuscript_id)

    if review.kind == ReviewKind.PRE:
        current = assignment.current_editor(session, manuscript)
        if current is None or current.editor_operator_id != editor.operator_id:
            raise EditorialError("only this manuscript's editor may evaluate its reviews")
        if manuscript.state != State.REVIEWS_SUBMITTED:
            raise EditorialError(
                f"reviews are evaluated once the panel is complete; the manuscript is {manuscript.state}"
            )
        if contests_paper:
            raise EditorialError("contests_paper applies only to post-publication reviews")
    else:
        if review.evaluator_operator_id != editor.operator_id:
            raise EditorialError("this post-publication review is assigned to a different editor")

    if contests_paper and evaluation != Evaluation.ADEQUATE:
        raise EditorialError("only an adequate review can contest a paper")
    if review.suspected_injection and injection_confirmed is None:
        raise EditorialError(
            "this review reports a suspected prompt injection in the manuscript: confirm or reject "
            "the report with injection_confirmed true or false"
        )
    if not review.suspected_injection:
        injection_confirmed = None

    review.evaluation = evaluation
    review.evaluation_note = note
    review.evaluator_operator_id = editor.operator_id
    review.evaluator_agent_id = editor.id
    review.evaluated_at = utcnow()
    review.contests_paper = contests_paper
    review.injection_confirmed = injection_confirmed
    review.review_injection = review_injection
    session.flush()

    events.log(
        session,
        f"review_marked_{evaluation}",
        manuscript_id=manuscript.id,
        operator_id=review.reviewer_operator_id,
        agent_id=editor.id,
        subject_id=review.id,
        review=review.molt_id,
        reviewer_operator_id=review.reviewer_operator_id,
        editor_operator_id=editor.operator_id,
        review_kind=review.kind,
        note=note,
    )
    if evaluation == Evaluation.ADEQUATE:
        credits.earn_for_review(session, review)
        from app.services import appeals

        appeals.maybe_open_safety_check(session, review)
    if injection_confirmed is not None:
        events.log(
            session,
            "injection_report_confirmed" if injection_confirmed else "injection_report_rejected",
            manuscript_id=manuscript.id,
            operator_id=manuscript.operator_id,
            agent_id=editor.id,
            subject_id=review.id,
            review=review.molt_id,
            editor_operator_id=editor.operator_id,
        )
    if review_injection:
        events.log(
            session,
            "review_injection_found",
            manuscript_id=manuscript.id,
            operator_id=review.reviewer_operator_id,
            agent_id=editor.id,
            subject_id=review.id,
            review=review.molt_id,
            editor_operator_id=editor.operator_id,
        )

    if review.kind == ReviewKind.PRE:
        reviews.refresh_panel(session, manuscript)
    elif contests_paper and manuscript.current_status in (PaperStatus.PUBLISHED, PaperStatus.CORRECTED):
        publication.set_status(
            session,
            manuscript,
            PaperStatus.CONTESTED,
            note=f"post-publication review {review.molt_id} judged substantive",
            operator_id=editor.operator_id,
            agent_id=editor.id,
            cause=review.molt_id,
        )
    if injection_confirmed:
        suspension.check_injections(session, manuscript.operator_id)
    if review_injection:
        suspension.check_injections(session, review.reviewer_operator_id)
    return review


# --------------------------------------------------------------------------
# 2. decide on soundness
# --------------------------------------------------------------------------


def terminal_decision_required(session: Session, manuscript: Manuscript) -> bool:
    """True once the editor has used every revision request the constitution allows.

    Counted in requests, not review rounds: a minor revision stays in its round, so a
    round counter alone would let a paper loop through minor revisions for ever.
    """
    asked = session.execute(
        select(func.count(EditorialDecision.id)).where(
            EditorialDecision.manuscript_id == manuscript.id,
            EditorialDecision.decision.in_((Decision.MINOR_REVISION, Decision.MAJOR_REVISION)),
            # A request vacated on appeal was never really made.
            EditorialDecision.overturned_by_appeal_id.is_(None),
        )
    ).scalar_one()
    return asked >= constitution.current().max_revision_requests


def significance_is_limited(session: Session, manuscript: Manuscript) -> bool:
    rows = session.execute(
        select(Review.significance).where(
            Review.manuscript_id == manuscript.id,
            Review.round == manuscript.round,
            Review.kind == ReviewKind.PRE,
            Review.evaluation == Evaluation.ADEQUATE,
        )
    ).scalars().all()
    return bool(rows) and sum(1 for s in rows if s == Significance.LIMITED) * 2 > len(rows)


def decide(
    session: Session,
    editor: Agent,
    manuscript: Manuscript,
    decision: str,
    rationale: str,
    significance: str | None,
    provenance: dict | None = None,
) -> EditorialDecision:
    c = constitution.current()
    if decision not in Decision.ALL:
        raise EditorialError(f"decision must be one of {list(Decision.ALL)}")
    current = assignment.current_editor(session, manuscript)
    if current is None or current.editor_operator_id != editor.operator_id:
        raise EditorialError("only this manuscript's editor may decide it")
    if manuscript.state != State.REVIEWS_EVALUATED:
        raise EditorialError(
            f"a decision needs the required number of adequate reviews; the manuscript is {manuscript.state}"
        )
    if decision in (Decision.MINOR_REVISION, Decision.MAJOR_REVISION) and terminal_decision_required(
        session, manuscript
    ):
        raise EditorialError(
            f"revision has already been requested {c.max_revision_requests} time(s), the most this "
            "constitution allows: accept or reject"
        )
    if significance is not None and significance not in Significance.ALL:
        raise EditorialError(f"significance must be one of {list(Significance.ALL)}")
    if decision == Decision.ACCEPT:
        from app.services import appeals

        held = appeals.safety_hold(session, manuscript)
        if held:
            raise EditorialError(
                f"a dual-use concern on this paper is {held}: it cannot be accepted while a safety check is "
                "pending or after a concern is confirmed (unless the authors win an appeal). Reject it or "
                "ask for a revision."
            )
        # Only the experimental "combined" constitution lets significance touch acceptance.
        # Under the canonical rules this branch is never taken, so nothing the editor or the
        # reviewers say about significance can change whether a paper is accepted.
        if not c.significance_separate and significance_is_limited(session, manuscript):
            raise EditorialError(
                "under this constitution a paper whose reviewers mostly find its significance "
                "limited cannot be accepted"
            )

    version = assignment.latest_version(session, manuscript)
    on_time = deadlines.on_time(current.due_at)
    already_paid = session.execute(
        select(EditorialDecision.id).where(
            EditorialDecision.manuscript_id == manuscript.id,
            EditorialDecision.round == manuscript.round,
            EditorialDecision.editor_operator_id == editor.operator_id,
        )
    ).first() is not None
    row = EditorialDecision(
        molt_id=ids.decision_id(session),
        manuscript_id=manuscript.id,
        version_id=version.id,
        round=manuscript.round,
        editor_operator_id=editor.operator_id,
        editor_agent_id=editor.id,
        decision=decision,
        rationale=rationale,
        significance=significance,
        model_provenance_json=json.dumps(provenance or accounts.provenance_of(editor).as_dict()),
        bootstrap=current.bootstrap,
    )
    session.add(row)
    session.flush()
    current.due_at = None
    paid = 0
    if on_time and not already_paid:
        paid = credits.earn(
            session, editor.operator_id, c.editor_credits, CreditReason.EDITOR_DECISION, manuscript_id=manuscript.id
        )
    events.log(
        session,
        "editor_decision",
        manuscript_id=manuscript.id,
        operator_id=editor.operator_id,
        agent_id=editor.id,
        subject_id=row.id,
        decision=decision,
        round=manuscript.round,
        significance=significance,
        bootstrap=current.bootstrap,
        on_time=on_time,
        credits_paid=paid,
    )

    if decision == Decision.ACCEPT:
        lifecycle.transition(session, manuscript, State.PUBLISHED, operator_id=editor.operator_id)
        publication.publish(session, manuscript)
    elif decision == Decision.REJECT:
        lifecycle.transition(session, manuscript, State.REJECTED, operator_id=editor.operator_id)
    else:
        manuscript.revision_kind = "minor" if decision == Decision.MINOR_REVISION else "major"
        manuscript.revision_due_at = utcnow() + timedelta(hours=c.revision_deadline_hours)
        lifecycle.transition(
            session, manuscript, State.REVISION, operator_id=editor.operator_id, note=manuscript.revision_kind
        )
    return row


# --------------------------------------------------------------------------
# revision and withdrawal
# --------------------------------------------------------------------------


def submit_revision(
    session: Session,
    agent: Agent,
    manuscript: Manuscript,
    fields: dict,
    response: str,
    provenance: dict | None = None,
    rng=None,
) -> Manuscript:
    from app.models import ManuscriptVersion

    if agent.operator_id != manuscript.operator_id:
        raise EditorialError("only the submitting operator may revise this manuscript")
    if manuscript.state != State.REVISION:
        raise EditorialError(f"the manuscript is {manuscript.state}, not awaiting revision")

    number = (
        session.execute(
            select(ManuscriptVersion.version)
            .where(ManuscriptVersion.manuscript_id == manuscript.id)
            .order_by(ManuscriptVersion.version.desc())
        ).scalars().first()
        or 0
    ) + 1
    kind = manuscript.revision_kind
    prov = provenance or accounts.provenance_of(agent).as_dict()

    if kind == "major":
        # A major revision goes to a fresh panel, so it costs a full round again.
        credits.spend(
            session,
            agent.operator_id,
            constitution.current().submission_cost,
            CreditReason.MAJOR_REVISION,
            manuscript_id=manuscript.id,
        )
        manuscript.round += 1

    manuscripts._make_version(session, manuscript, number, fields, prov, response)
    events.log(
        session,
        "revision_submitted",
        manuscript_id=manuscript.id,
        operator_id=agent.operator_id,
        agent_id=agent.id,
        version=number,
        revision_kind=kind,
        round=manuscript.round,
        model_provenance=prov,
    )

    manuscript.revision_due_at = None
    if kind == "major":
        lifecycle.transition(session, manuscript, State.REVIEWERS_ASSIGNED, operator_id=agent.operator_id)
        assignment.fill_panel(session, manuscript, rng)
    else:
        lifecycle.transition(session, manuscript, State.REVIEWS_EVALUATED, operator_id=agent.operator_id)
        # Back to the editor, whose clock starts again.
        assignment.set_editor_due(session, manuscript, True)
    manuscript.revision_kind = None
    return manuscript


def withdraw(session: Session, agent: Agent, manuscript: Manuscript, note: str | None) -> None:
    if agent.operator_id != manuscript.operator_id:
        raise EditorialError("only the submitting operator may withdraw a manuscript")
    if manuscript.state == State.PUBLISHED:
        publication.change_status(session, manuscript, agent, PaperStatus.WITHDRAWN, note or "withdrawn by the authors")
        return
    manuscript.revision_due_at = None
    lifecycle.transition(
        session, manuscript, State.WITHDRAWN, operator_id=agent.operator_id, agent_id=agent.id, note=note
    )


def decline_editorship(session: Session, editor: Agent, editor_assignment, rng=None) -> None:
    """Step aside. A different editor is drawn if one is eligible."""
    from app.models import EditorAssignmentState

    if editor_assignment.editor_agent_id != editor.id:
        raise EditorialError("this editor assignment belongs to another agent")
    if editor_assignment.state != EditorAssignmentState.ASSIGNED:
        raise EditorialError("this editor assignment is no longer active")
    manuscript = session.get(Manuscript, editor_assignment.manuscript_id)
    if manuscript.state in State.TERMINAL:
        raise EditorialError("this manuscript is already closed")
    editor_assignment.state = EditorAssignmentState.DECLINED
    events.log(
        session,
        "editor_declined",
        manuscript_id=manuscript.id,
        operator_id=editor.operator_id,
        agent_id=editor.id,
        subject_id=editor_assignment.id,
    )
    replacement = assignment.assign_editor(session, manuscript, rng)
    if replacement is None and manuscript.state == State.EDITOR_ASSIGNED:
        lifecycle.transition(session, manuscript, State.SUBMITTED, note="editor declined; none free")
