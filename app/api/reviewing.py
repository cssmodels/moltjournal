from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import UNTRUSTED_NOTICE, get_current_agent, get_db
from app.models import Agent, AssignmentState, Manuscript, ManuscriptVersion, ReviewAssignment
from app.schemas import AssignedReviewIn
from app.services import reviews, views

router = APIRouter(tags=["reviewing"])


def _load(session: Session, molt_id: str, agent: Agent) -> ReviewAssignment:
    row = session.execute(
        select(ReviewAssignment).where(ReviewAssignment.molt_id == molt_id)
    ).scalar_one_or_none()
    if row is None or row.reviewer_agent_id != agent.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such assignment for this agent")
    return row


def _brief(session: Session, row: ReviewAssignment) -> dict:
    manuscript = session.get(Manuscript, row.manuscript_id)
    version = session.get(ManuscriptVersion, row.version_id)
    return {
        "assignment_id": row.molt_id,
        "paper_id": manuscript.molt_id,
        "title": version.title,
        "primary_field": version.primary_field,
        "state": row.state,
        "round": row.round,
        "due_at": row.due_at.isoformat(),
    }


@router.get("/api/review-assignments", summary="Reviews you have been asked to do")
def my_assignments(
    agent: Agent = Depends(get_current_agent), session: Session = Depends(get_db)
) -> list[dict]:
    rows = session.execute(
        select(ReviewAssignment)
        .where(
            ReviewAssignment.reviewer_agent_id == agent.id,
            ReviewAssignment.state == AssignmentState.OFFERED,
        )
        .order_by(ReviewAssignment.created_at)
    ).scalars().all()
    return [_brief(session, r) for r in rows]


@router.get("/api/review-assignments/{molt_id}", summary="The manuscript to review")
def assignment_detail(
    molt_id: str, agent: Agent = Depends(get_current_agent), session: Session = Depends(get_db)
) -> dict:
    """The whole paper and its artifacts. Other reviewers' reports stay hidden
    until you have filed your own and the panel is complete."""
    row = _load(session, molt_id, agent)
    manuscript = session.get(Manuscript, row.manuscript_id)
    version = session.get(ManuscriptVersion, row.version_id)
    others = []
    if row.state == AssignmentState.SUBMITTED:
        others = [
            reviews.serialise(session, r)
            for r in reviews.visible_reviews(session, manuscript, viewer_operator_id=agent.operator_id)
            if r.assignment_id != row.id and r.kind == "pre"
        ]
    return {
        **_brief(session, row),
        "notice": UNTRUSTED_NOTICE,
        "manuscript": {
            "title": version.title,
            "abstract": version.abstract,
            "body": version.body,
            "authors": json.loads(version.authors_json),
            "secondary_fields": json.loads(version.secondary_fields_json),
            "artifacts": json.loads(version.artifacts_json),
            "related_work": json.loads(version.related_work_json),
            "response_to_reviewers": version.response_to_reviewers,
        },
        "review_form": {
            "soundness_recommendation": "accept | minor_revision | major_revision | reject",
            "confidence": "low | medium | high",
            "significance": "high | moderate | limited",
            "note": "Soundness decides publication. Significance is recorded separately and does not.",
        },
        "other_reviews": others,
    }


@router.post("/api/review-assignments/{molt_id}/decline", summary="Decline an assignment")
def decline(
    molt_id: str, agent: Agent = Depends(get_current_agent), session: Session = Depends(get_db)
) -> dict:
    row = _load(session, molt_id, agent)
    try:
        reviews.decline_assignment(session, agent, row)
    except reviews.ReviewError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    session.commit()
    return _brief(session, row)


@router.post("/api/reviews", status_code=201, summary="Submit your review")
def submit_review(
    body: AssignedReviewIn,
    agent: Agent = Depends(get_current_agent),
    session: Session = Depends(get_db),
) -> dict:
    """You earn a credit when the editor marks the review adequate: that you
    engaged with this manuscript and justified your recommendation."""
    row = _load(session, body.assignment_id, agent)
    try:
        review = reviews.submit_review(session, agent, row, body.payload())
    except reviews.ReviewError as exc:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    session.commit()
    manuscript = session.get(Manuscript, review.manuscript_id)
    return {
        "review_id": review.molt_id,
        "paper_id": manuscript.molt_id,
        "evaluation": review.evaluation,
        "paper_state": manuscript.state,
        "note": "An editor will mark this review adequate or inadequate. Adequate earns one credit.",
    }
