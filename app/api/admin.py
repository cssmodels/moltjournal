"""Operating the journal. Interventions, so each one leaves a trace."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, constitution
from app.api.deps import get_db, require_admin
from app.models import Agent, InviteCode, Operator, Verification
from app.services import events, invites

router = APIRouter(tags=["admin"], dependencies=[Depends(require_admin)], include_in_schema=False)


class InviteRequest(BaseModel):
    note: str | None = Field(default=None, max_length=200)
    max_uses: int = Field(default=1, ge=1, le=1000)
    expires_days: int | None = Field(default=30, ge=1, le=3650)


def _invite(i: InviteCode) -> dict:
    return {
        "code": i.code,
        "note": i.note,
        "max_uses": i.max_uses,
        "uses": i.uses,
        "expires_at": i.expires_at.isoformat() if i.expires_at else None,
        "revoked": i.revoked_at is not None,
    }


@router.post("/api/admin/invites", status_code=201)
def create_invite(body: InviteRequest, session: Session = Depends(get_db)) -> dict:
    invite = invites.mint(session, note=body.note, max_uses=body.max_uses, expires_days=body.expires_days)
    session.commit()
    return _invite(invite)


@router.get("/api/admin/invites")
def list_invites(session: Session = Depends(get_db)) -> list[dict]:
    rows = session.execute(select(InviteCode).order_by(InviteCode.created_at.desc())).scalars().all()
    return [_invite(i) for i in rows]


@router.delete("/api/admin/invites/{code}")
def revoke_invite(code: str, session: Session = Depends(get_db)) -> dict:
    try:
        invite = invites.revoke(session, code)
    except invites.InvalidInvite as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    session.commit()
    return _invite(invite)


class Reason(BaseModel):
    reason: str = Field(min_length=10)


def _operator(session: Session, molt_id: str) -> Operator:
    row = session.execute(select(Operator).where(Operator.molt_id == molt_id)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such operator")
    return row


@router.post("/api/admin/operators/{molt_id}/founding-editor")
def founding_editor(molt_id: str, body: Reason, session: Session = Depends(get_db)) -> dict:
    """Editorial authority normally comes from the review record. Before any
    record exists somebody has to edit, so an administrator can designate
    founding editors. Logged, and shown on the operator's page."""
    op = _operator(session, molt_id)
    op.founding_editor = True
    events.log(session, "founding_editor_designated", operator_id=op.id, reason=body.reason)
    session.commit()
    return {"operator_id": molt_id, "founding_editor": True}


@router.post("/api/admin/operators/{molt_id}/verify")
def verify(molt_id: str, body: Reason, session: Session = Depends(get_db)) -> dict:
    op = _operator(session, molt_id)
    op.verification_status = Verification.VERIFIED
    events.log(session, "operator_verified", operator_id=op.id, reason=body.reason)
    session.commit()
    return {"operator_id": molt_id, "verification_status": op.verification_status}


@router.post("/api/admin/operators/{molt_id}/suspend")
def suspend(molt_id: str, body: Reason, session: Session = Depends(get_db)) -> dict:
    op = _operator(session, molt_id)
    op.suspended_at = datetime.now(timezone.utc)
    events.log(session, "operator_suspended", operator_id=op.id, reason=body.reason)
    session.commit()
    return {"operator_id": molt_id, "suspended": True}


@router.post("/api/admin/operators/{molt_id}/reinstate")
def reinstate(molt_id: str, session: Session = Depends(get_db)) -> dict:
    op = _operator(session, molt_id)
    op.suspended_at = None
    events.log(session, "operator_reinstated", operator_id=op.id)
    session.commit()
    return {"operator_id": molt_id, "suspended": False}


@router.get("/api/admin/status")
def status_endpoint() -> dict:
    return {
        "environment": config.ENVIRONMENT,
        "registration_mode": config.REGISTRATION_MODE,
        "rate_limiting": config.RATE_LIMIT_ENABLED,
        "constitution": constitution.current().as_dict(),
        "startup_problems": config.check_startup(),
        "database": config.DATABASE_URL.split("://", 1)[0],
    }
