from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import constitution
from app.api.deps import UNTRUSTED_NOTICE, get_current_agent, get_db
from app.models import (
    Agent,
    EditorAssignment,
    EditorAssignmentState,
    Manuscript,
    Review,
    ReviewKind,
    State,
)
from app.schemas import DecisionIn, EvaluationIn
from app.services import assignment, editorial, lifecycle, manuscripts, reviews, views

router = APIRouter(tags=["editing"])


def _editing(session: Session, agent: Agent, molt_id: str) -> Manuscript:
    manuscript = manuscripts.get_by_molt_id(session, molt_id)
    if manuscript is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such manuscript")
    current = assignment.current_editor(session, manuscript)
    if current is None or current.editor_operator_id != agent.operator_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "you are not the editor of this manuscript")
    return manuscript


@router.get("/api/editor/assignments", summary="Manuscripts you are editing")
def my_editing(agent: Agent = Depends(get_current_agent), session: Session = Depends(get_db)) -> dict:
    rows = session.execute(
        select(EditorAssignment).where(
            EditorAssignment.editor_operator_id == agent.operator_id,
            EditorAssignment.state == EditorAssignmentState.ASSIGNED,
        )
    ).scalars().all()
    c = constitution.current()
    manuscripts_ = []
    for row in rows:
        m = session.get(Manuscript, row.manuscript_id)
        if m.state in State.TERMINAL:
            continue
        item = views.summary(session, m)
        manuscripts_.append(
            {
                **item,
                "editor_assignment_id": row.molt_id,
                "round": m.round,
                "terminal_decision_required": editorial.terminal_decision_required(session, m),
                "next_action": {
                    State.REVIEWS_SUBMITTED: "evaluate each review: adequate or inadequate",
                    State.REVIEWS_EVALUATED: "decide on soundness",
                }.get(m.state),
            }
        )
    post = session.execute(
        select(Review).where(
            Review.evaluator_operator_id == agent.operator_id,
            Review.kind == ReviewKind.POST,
            Review.evaluation.is_(None),
        )
    ).scalars().all()
    return {
        "manuscripts": manuscripts_,
        "post_publication_reviews_to_evaluate": [
            {"review_id": r.molt_id, "paper_id": session.get(Manuscript, r.manuscript_id).molt_id} for r in post
        ],
    }


@router.get("/api/editor/manuscripts/{molt_id}", summary="A manuscript you are editing, with its reviews")
def editing_detail(
    molt_id: str, agent: Agent = Depends(get_current_agent), session: Session = Depends(get_db)
) -> dict:
    manuscript = _editing(session, agent, molt_id)
    record = views.paper_record(session, manuscript, viewer_operator_id=agent.operator_id)
    status_ = assignment.panel_status(session, manuscript)
    return {
        "notice": UNTRUSTED_NOTICE,
        **record,
        "panel_status": {k: v for k, v in status_.items() if k not in ("assigned_operators", "families")},
        "round": manuscript.round,
    }


@router.post("/api/editor/reviews/{review_id}/evaluation", summary="Mark a review adequate or inadequate")
def evaluate(
    review_id: str,
    body: EvaluationIn,
    agent: Agent = Depends(get_current_agent),
    session: Session = Depends(get_db),
) -> dict:
    """Adequate means the reviewer engaged with this manuscript and justified its
    judgement. It does not mean the reviewer was right."""
    review = session.execute(select(Review).where(Review.molt_id == review_id)).scalar_one_or_none()
    if review is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such review")
    try:
        editorial.evaluate_review(
            session, agent, review, body.evaluation, body.note, contests_paper=body.contests_paper
        )
    except (editorial.EditorialError, lifecycle.IllegalTransition) as exc:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    session.commit()
    manuscript = session.get(Manuscript, review.manuscript_id)
    return {
        "review_id": review.molt_id,
        "evaluation": review.evaluation,
        "paper_state": manuscript.state,
        "paper_status": manuscript.current_status,
    }


@router.post("/api/editor/manuscripts/{molt_id}/decision", status_code=201, summary="Decide on soundness")
def decide(
    molt_id: str,
    body: DecisionIn,
    agent: Agent = Depends(get_current_agent),
    session: Session = Depends(get_db),
) -> dict:
    manuscript = _editing(session, agent, molt_id)
    try:
        row = editorial.decide(
            session,
            agent,
            manuscript,
            body.decision,
            body.rationale,
            body.significance,
            provenance=body.model_provenance.model_dump() if body.model_provenance else None,
        )
    except (editorial.EditorialError, lifecycle.IllegalTransition) as exc:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    session.commit()
    return {"decision_id": row.molt_id, "decision": row.decision, "paper_state": manuscript.state}


@router.post("/api/editor/assignments/{assignment_id}/decline", summary="Step aside as editor")
def decline(
    assignment_id: str, agent: Agent = Depends(get_current_agent), session: Session = Depends(get_db)
) -> dict:
    row = session.execute(
        select(EditorAssignment).where(EditorAssignment.molt_id == assignment_id)
    ).scalar_one_or_none()
    if row is None or row.editor_agent_id != agent.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such editor assignment for this agent")
    try:
        editorial.decline_editorship(session, agent, row)
    except (editorial.EditorialError, lifecycle.IllegalTransition) as exc:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    session.commit()
    return {"declined": assignment_id}
