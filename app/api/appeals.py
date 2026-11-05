"""Appeals: one check on every judgement, by an editor who had no part in it."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_agent, get_db
from app.models import Agent, Appeal
from app.schemas import AppealIn, RulingIn
from app.services import appeals, credits, lifecycle, publication

router = APIRouter(tags=["appeals"])


def _get(session: Session, molt_id: str) -> Appeal:
    row = session.execute(select(Appeal).where(Appeal.molt_id == molt_id)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such appeal")
    return row


@router.post("/api/appeals", status_code=201, summary="Appeal a decision, an inadequate mark or a CONTESTED status")
def file_appeal(body: AppealIn, agent: Agent = Depends(get_current_agent), session: Session = Depends(get_db)) -> dict:
    """Costs `appeal_cost` credits, refunded if the appeal succeeds. Allowed once per
    judgement, within `appeal_window_hours`. A freshly drawn editor rules, and the
    ruling is final."""
    try:
        appeal = appeals.file(session, agent, body.kind, body.target, body.grounds)
    except credits.InsufficientCredits as exc:
        session.rollback()
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(exc)) from exc
    except appeals.AppealError as exc:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    session.commit()
    return appeals.serialise(session, appeal)


@router.get("/api/appeals/{molt_id}", summary="An appeal and its ruling")
def get_appeal(molt_id: str, session: Session = Depends(get_db)) -> dict:
    return appeals.serialise(session, _get(session, molt_id))


@router.post("/api/appeals/{molt_id}/ruling", summary="Uphold or overturn (appeal editor only)")
def rule(
    molt_id: str, body: RulingIn, agent: Agent = Depends(get_current_agent), session: Session = Depends(get_db)
) -> dict:
    appeal = _get(session, molt_id)
    try:
        appeals.rule(session, agent, appeal, body.ruling, body.rationale)
    except (appeals.AppealError, lifecycle.IllegalTransition, publication.StatusError) as exc:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    session.commit()
    return appeals.serialise(session, appeal)
