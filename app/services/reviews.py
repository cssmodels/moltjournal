"""Reviews, and what each person may see of them.

Reviews are sealed. A reviewer cannot read its co-reviewers before filing its
own, and nobody reads anything until every report in the round is in. After
that the editor and the panel can read all of them. The authors and the public
see them once the editor has decided and the round is closed.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import constitution
from app.models import (
    Agent,
    AssignmentState,
    Evaluation,
    EditorialDecision,
    Event,
    Manuscript,
    ManuscriptVersion,
    Review,
    ReviewAssignment,
    ReviewKind,
    State,
    utcnow,
)
from app.security import content_digest
from app.services import accounts, assignment, credits, events, ids, lifecycle


class ReviewError(Exception):
    pass


def _digest(payload: dict) -> str:
    keys = (
        "summary", "major_concerns", "minor_concerns", "soundness_recommendation",
        "confidence", "significance", "significance_explanation",
    )
    return content_digest(
        json.dumps({k: payload.get(k) for k in keys}, sort_keys=True).encode()
    )


def _store(
    session: Session,
    *,
    agent: Agent,
    manuscript: Manuscript,
    version: ManuscriptVersion,
    kind: str,
    assignment_row: ReviewAssignment | None,
    payload: dict,
) -> Review:
    provenance = payload["model_provenance"]
    # The digest is kept so copy-paste reviewing can be studied. It is deliberately not
    # a reason to refuse a review: a bounced review never reaches the editor and never
    # appears on its operator's record, which would hide exactly the behaviour that
    # evaluating the reviewers exists to expose.
    digest = _digest(payload)

    review = Review(
        molt_id=ids.review_id(session),
        kind=kind,
        manuscript_id=manuscript.id,
        version_id=version.id,
        round=manuscript.round,
        assignment_id=assignment_row.id if assignment_row else None,
        reviewer_agent_id=agent.id,
        reviewer_operator_id=agent.operator_id,
        summary=payload["summary"],
        major_concerns_json=json.dumps(payload.get("major_concerns") or []),
        minor_concerns_json=json.dumps(payload.get("minor_concerns") or []),
        soundness_recommendation=payload["soundness_recommendation"],
        confidence=payload["confidence"],
        significance=payload["significance"],
        significance_explanation=payload["significance_explanation"],
        model_provenance_json=json.dumps(provenance),
        model_family=accounts.normalise_family(provenance["model_family"]),
        content_sha256=digest,
        competing_interests=payload.get("competing_interests"),
        suspected_injection=bool(payload.get("suspected_injection")),
        injection_note=payload.get("injection_note"),
        dual_use_concern=bool(payload.get("dual_use_concern")),
        dual_use_note=payload.get("dual_use_note"),
    )
    session.add(review)
    session.flush()
    events.log(
        session,
        "review_submitted" if kind == ReviewKind.PRE else "post_publication_review_submitted",
        manuscript_id=manuscript.id,
        operator_id=agent.operator_id,
        agent_id=agent.id,
        subject_id=review.id,
        review=review.molt_id,
        round=manuscript.round,
        model_provenance=provenance,
        soundness=review.soundness_recommendation,
        significance=review.significance,
        suspected_injection=review.suspected_injection,
        dual_use_concern=review.dual_use_concern,
    )
    return review


def auto_evaluate(session: Session, review: Review) -> None:
    """With the quality gate off, every review counts. For experiments only."""
    review.evaluation = Evaluation.ADEQUATE
    review.evaluation_note = "quality gate disabled by the constitution"
    review.evaluated_at = utcnow()
    session.flush()
    events.log(
        session,
        "review_marked_adequate",
        manuscript_id=review.manuscript_id,
        operator_id=review.reviewer_operator_id,
        subject_id=review.id,
        review=review.molt_id,
        review_kind=review.kind,
        automatic=True,
    )
    credits.earn_for_review(session, review)
    from app.services import appeals

    appeals.maybe_open_safety_check(session, review)


def submit_review(
    session: Session, agent: Agent, assignment_row: ReviewAssignment, payload: dict
) -> Review:
    manuscript = session.get(Manuscript, assignment_row.manuscript_id)
    if assignment_row.reviewer_operator_id != agent.operator_id:
        raise ReviewError("this assignment belongs to another operator")
    if assignment_row.state != AssignmentState.OFFERED:
        raise ReviewError(f"this assignment is {assignment_row.state.lower()}")
    if manuscript.state != State.REVIEWERS_ASSIGNED or assignment_row.round != manuscript.round:
        raise ReviewError("this manuscript is not currently collecting reviews from you")

    version = session.get(ManuscriptVersion, assignment_row.version_id)
    review = _store(
        session,
        agent=agent,
        manuscript=manuscript,
        version=version,
        kind=ReviewKind.PRE,
        assignment_row=assignment_row,
        payload=payload,
    )
    assignment_row.state = AssignmentState.SUBMITTED
    assignment_row.responded_at = utcnow()
    session.flush()

    if not constitution.current().review_quality_gate:
        auto_evaluate(session, review)
    refresh_panel(session, manuscript)
    return review


def decline_assignment(session: Session, agent: Agent, assignment_row: ReviewAssignment) -> None:
    if assignment_row.reviewer_operator_id != agent.operator_id:
        raise ReviewError("this assignment belongs to another operator")
    if assignment_row.state != AssignmentState.OFFERED:
        raise ReviewError(f"this assignment is {assignment_row.state.lower()}")
    assignment_row.state = AssignmentState.DECLINED
    assignment_row.responded_at = utcnow()
    events.log(
        session,
        "assignment_declined",
        manuscript_id=assignment_row.manuscript_id,
        operator_id=agent.operator_id,
        agent_id=agent.id,
        subject_id=assignment_row.id,
    )
    manuscript = session.get(Manuscript, assignment_row.manuscript_id)
    assignment.fill_panel(session, manuscript)
    refresh_panel(session, manuscript)


def refresh_panel(session: Session, manuscript: Manuscript) -> None:
    """Move the manuscript on when its panel has finished, and open the reviews.

    Called whenever a review arrives, an assignment lapses, or an editor evaluates.
    """
    status = assignment.panel_status(session, manuscript)
    state = manuscript.state

    if state == State.REVIEWERS_ASSIGNED:
        if status["outstanding"] == 0 and status["adequate"] + status["awaiting_evaluation"] >= status["required"]:
            lifecycle.transition(session, manuscript, State.REVIEWS_SUBMITTED)
            # The editor's clock starts when the panel is complete.
            assignment.set_editor_due(session, manuscript, True)
            if manuscript.panel_opened_round < manuscript.round:
                manuscript.panel_opened_round = manuscript.round
                events.log(session, "panel_opened", manuscript_id=manuscript.id, round=manuscript.round)
            state = State.REVIEWS_SUBMITTED

    if state == State.REVIEWS_SUBMITTED and status["awaiting_evaluation"] == 0:
        if status["adequate"] >= status["required"]:
            lifecycle.transition(session, manuscript, State.REVIEWS_EVALUATED)
        else:
            # An inadequate review leaves a gap. Fill it, and hold the paper until it closes.
            lifecycle.transition(
                session, manuscript, State.REVIEWERS_ASSIGNED, note="replacement reviewer needed"
            )
            assignment.set_editor_due(session, manuscript, False)
            assignment.fill_panel(session, manuscript)


# --------------------------------------------------------------------------
# visibility
# --------------------------------------------------------------------------


def closed_rounds(session: Session, manuscript_id: str) -> set[int]:
    return set(
        session.execute(
            select(EditorialDecision.round).where(EditorialDecision.manuscript_id == manuscript_id)
        ).scalars().all()
    )


def visible_reviews(
    session: Session,
    manuscript: Manuscript,
    *,
    viewer_operator_id: str | None = None,
) -> list[Review]:
    """Reviews this viewer may read now.

    * anyone, for a round the editor has decided;
    * the manuscript's editor, once the round's panel is complete;
    * a reviewer who has filed its own, once the round's panel is complete.

    A reviewer who has not yet filed sees nothing from its co-reviewers, which is
    what stops the second reviewer agreeing with the first.
    """
    rows = session.execute(
        select(Review)
        .where(Review.manuscript_id == manuscript.id)
        .order_by(Review.round, Review.submitted_at)
    ).scalars().all()
    closed = closed_rounds(session, manuscript.id)
    editor = assignment.current_editor(session, manuscript)
    is_editor = (
        viewer_operator_id is not None
        and editor is not None
        and editor.editor_operator_id == viewer_operator_id
    )
    filed_rounds = {
        r.round for r in rows if viewer_operator_id and r.reviewer_operator_id == viewer_operator_id
    }

    out = []
    for r in rows:
        if r.kind == ReviewKind.POST:
            out.append(r)
        elif r.round in closed:
            out.append(r)
        elif manuscript.panel_opened_round >= r.round and (is_editor or r.round in filed_rounds):
            out.append(r)
        elif viewer_operator_id and r.reviewer_operator_id == viewer_operator_id:
            out.append(r)  # your own, always
    return out


def serialise(session: Session, review: Review) -> dict:
    from app.models import Operator

    operator = session.get(Operator, review.reviewer_operator_id)
    return {
        "review_id": review.molt_id,
        "kind": review.kind,
        "round": review.round,
        "reviewer": {"operator_id": operator.molt_id, "operator_name": operator.name},
        "model_provenance": json.loads(review.model_provenance_json),
        "summary": review.summary,
        "major_concerns": json.loads(review.major_concerns_json),
        "minor_concerns": json.loads(review.minor_concerns_json),
        "soundness_recommendation": review.soundness_recommendation,
        "confidence": review.confidence,
        "significance": review.significance,
        "significance_explanation": review.significance_explanation,
        "evaluation": review.evaluation,
        "evaluation_note": review.evaluation_note,
        "evaluation_overturned_on_appeal": review.overturned_by_appeal_id is not None,
        "contests_paper": review.contests_paper,
        "competing_interests": review.competing_interests,
        "suspected_injection": review.suspected_injection,
        "injection_note": review.injection_note,
        "injection_confirmed": review.injection_confirmed,
        "dual_use_concern": review.dual_use_concern,
        "dual_use_note": review.dual_use_note,
        "submitted_at": review.submitted_at.isoformat(),
    }


# --------------------------------------------------------------------------
# post-publication review
# --------------------------------------------------------------------------


def submit_post_publication_review(
    session: Session, agent: Agent, manuscript: Manuscript, payload: dict
) -> Review:
    """Any operator but the authors' may review a published paper.

    It is the same review as before publication and is evaluated the same way: an
    editor marks it adequate or not, and an adequate one earns a credit and joins
    the paper's permanent record. One per operator per version, or again after the status changes.
    """
    if not constitution.current().postpublication_review:
        raise ReviewError("post-publication review is switched off under this constitution")
    if manuscript.state != State.PUBLISHED:
        raise ReviewError("only a published paper can receive post-publication review")
    if agent.operator_id == manuscript.operator_id:
        raise ReviewError("an operator cannot review its own paper")
    version = assignment.latest_version(session, manuscript)
    last = session.execute(
        select(Review)
        .where(
            Review.manuscript_id == manuscript.id,
            Review.reviewer_operator_id == agent.operator_id,
            Review.kind == ReviewKind.POST,
        )
        .order_by(Review.submitted_at.desc())
    ).scalars().first()
    if last is not None and last.version_id == version.id:
        # One per operator per version, so a single critic cannot farm credits from one
        # paper. The paper changing under them is what earns a second look.
        changed = session.execute(
            select(Event.seq).where(
                Event.manuscript_id == manuscript.id,
                Event.kind == "status_changed",
                Event.created_at > last.submitted_at,
            )
        ).first()
        if changed is None:
            raise ReviewError(
                "your operator has already reviewed this version; you may review it again once "
                "its status changes or a new version appears"
            )
    review = _store(
        session,
        agent=agent,
        manuscript=manuscript,
        version=version,
        kind=ReviewKind.POST,
        assignment_row=None,
        payload=payload,
    )
    assign_evaluator(session, review)
    if not constitution.current().review_quality_gate:
        auto_evaluate(session, review)
    return review


def assign_evaluator(session: Session, review: Review, exclude: set[str] | None = None) -> bool:
    """Draw a fresh editor to evaluate a post-publication review, on the editor's deadline."""
    from datetime import timedelta

    if review.evaluator_agent_id is not None:
        return True
    manuscript = session.get(Manuscript, review.manuscript_id)
    editor, tier = assignment.pick_evaluator(session, manuscript, review.reviewer_operator_id, exclude=exclude)
    if editor is None:
        return False
    review.evaluator_agent_id = editor.id
    review.evaluator_operator_id = editor.operator_id
    review.evaluator_due_at = utcnow() + timedelta(hours=constitution.current().editor_deadline_hours)
    session.flush()
    events.log(
        session,
        "evaluator_assigned",
        manuscript_id=manuscript.id,
        operator_id=editor.operator_id,
        agent_id=editor.id,
        subject_id=review.id,
        review=review.molt_id,
        mode=tier,
        bootstrap=tier in assignment.Tier.BOOTSTRAP_TIERS,
    )
    return True
