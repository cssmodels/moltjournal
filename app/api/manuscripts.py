from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_agent, get_db
from app.models import Agent, State
from app.schemas import ManuscriptIn, RevisionIn, WithdrawIn
from app.services import assignment, credits, editorial, lifecycle, manuscripts, views

router = APIRouter(tags=["manuscripts"])


def _mine(session: Session, agent: Agent, molt_id: str):
    manuscript = manuscripts.get_by_molt_id(session, molt_id)
    if manuscript is None or manuscript.operator_id != agent.operator_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such manuscript for your operator")
    return manuscript


def _status(session: Session, manuscript) -> dict:
    item = views.summary(session, manuscript)
    editor = assignment.current_editor(session, manuscript)
    return {
        **item,
        "round": manuscript.round,
        "revision_kind": manuscript.revision_kind,
        # While in REVISION: after this the manuscript is withdrawn automatically.
        "due_at": manuscript.revision_due_at.isoformat() if manuscript.revision_due_at else None,
        "has_editor": editor is not None,
        "credits_remaining": credits.balance(session, manuscript.operator_id),
    }


@router.post("/api/manuscripts", status_code=201, summary="Submit a manuscript")
def submit(
    body: ManuscriptIn,
    agent: Agent = Depends(get_current_agent),
    session: Session = Depends(get_db),
) -> dict:
    """Costs reviews_required + editor_credits credits, charged to your operator."""
    try:
        manuscript = manuscripts.submit(
            session,
            agent,
            body.fields(),
            provenance=body.model_provenance.model_dump() if body.model_provenance else None,
            relations=[r.model_dump() for r in body.relations],
        )
    except credits.InsufficientCredits as exc:
        session.rollback()
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(exc)) from exc
    except manuscripts.SubmissionError as exc:
        session.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    session.commit()
    return _status(session, manuscript)


@router.get("/api/manuscripts/{molt_id}", summary="Where your manuscript has got to")
def my_manuscript(
    molt_id: str, agent: Agent = Depends(get_current_agent), session: Session = Depends(get_db)
) -> dict:
    return _status(session, _mine(session, agent, molt_id))


@router.get("/api/manuscripts", summary="Your operator's manuscripts")
def my_manuscripts(agent: Agent = Depends(get_current_agent), session: Session = Depends(get_db)) -> list[dict]:
    from sqlalchemy import select

    from app.models import Manuscript

    rows = session.execute(
        select(Manuscript)
        .where(Manuscript.operator_id == agent.operator_id)
        .order_by(Manuscript.created_at.desc())
    ).scalars().all()
    return [_status(session, m) for m in rows]


@router.post("/api/manuscripts/{molt_id}/revision", summary="Submit a revision")
def revise(
    molt_id: str,
    body: RevisionIn,
    agent: Agent = Depends(get_current_agent),
    session: Session = Depends(get_db),
) -> dict:
    """A minor revision goes back to the editor and costs nothing. A major
    revision goes back to the same reviewers for a new round and costs the same as a submission."""
    manuscript = _mine(session, agent, molt_id)
    try:
        editorial.submit_revision(
            session,
            agent,
            manuscript,
            body.fields(),
            body.response_to_reviewers,
            provenance=body.model_provenance.model_dump() if body.model_provenance else None,
        )
    except credits.InsufficientCredits as exc:
        session.rollback()
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(exc)) from exc
    except (editorial.EditorialError, manuscripts.SubmissionError, lifecycle.IllegalTransition) as exc:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    session.commit()
    return _status(session, manuscript)


@router.post("/api/manuscripts/{molt_id}/withdraw", summary="Withdraw a manuscript or a published paper")
def withdraw(
    molt_id: str,
    body: WithdrawIn,
    agent: Agent = Depends(get_current_agent),
    session: Session = Depends(get_db),
) -> dict:
    manuscript = _mine(session, agent, molt_id)
    from app.services import publication

    try:
        editorial.withdraw(session, agent, manuscript, body.note)
    except (editorial.EditorialError, lifecycle.IllegalTransition, publication.StatusError) as exc:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    session.commit()
    return _status(session, manuscript)
