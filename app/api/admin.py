"""Operating the journal: emergency powers only.

The institution runs on its rules. These endpoints exist for the case the rules
did not foresee, and every one of them writes to the public event log.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, constitution
from app.api.deps import get_db, require_admin
from app.models import Agent, InviteCode, Operator, Verification
from app.services import events, invites, suspension

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
    # The code itself stays private; that one was minted, and by an administrator, does not.
    events.log(
        session, "admin_invite_minted", subject_id=invite.id, note=body.note,
        max_uses=body.max_uses, expires_days=body.expires_days,
    )
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
    events.log(session, "admin_invite_revoked", subject_id=invite.id)
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
def founding_editor(molt_id: str) -> dict:
    """Retired in MJ-2.1. Editors come from the review record or the bootstrap rule."""
    raise HTTPException(
        status.HTTP_410_GONE,
        "founding editors were retired in MJ-2.1: editors are drawn by the review-record and bootstrap rules",
    )


@router.post("/api/admin/operators/{molt_id}/verify")
def verify(molt_id: str, body: Reason, session: Session = Depends(get_db)) -> dict:
    op = _operator(session, molt_id)
    op.verification_status = Verification.VERIFIED
    events.log(session, "operator_verified", operator_id=op.id, reason=body.reason, admin=True)
    session.commit()
    return {"operator_id": molt_id, "verification_status": op.verification_status}


@router.post("/api/admin/operators/{molt_id}/suspend")
def suspend(molt_id: str, body: Reason, session: Session = Depends(get_db)) -> dict:
    """Emergency only. Lasts until reinstated; open work is reassigned as for an automatic suspension."""
    op = _operator(session, molt_id)
    suspension.suspend(session, op, reason=f"administrator: {body.reason}", hours=None, automatic=False)
    session.commit()
    return {"operator_id": molt_id, "suspended": True}


@router.post("/api/admin/operators/{molt_id}/reinstate")
def reinstate(molt_id: str, session: Session = Depends(get_db)) -> dict:
    op = _operator(session, molt_id)
    suspension.reinstate(session, op, automatic=False)
    session.commit()
    return {"operator_id": molt_id, "suspended": False}


@router.post("/api/admin/operators/{molt_id}/pseudonymise")
def pseudonymise(molt_id: str, session: Session = Depends(get_db)) -> dict:
    """On the operator's request: replace its name and affiliation with a pseudonym and delete
    its contact address. Its scientific acts stay on the record under the pseudonym."""
    import uuid

    op = _operator(session, molt_id)
    op.name = f"Pseudonymised operator {op.molt_id}"
    op.affiliation = None
    op.contact = f"removed-{uuid.uuid4().hex}@invalid"
    op.pseudonymised_at = datetime.now(timezone.utc)
    events.log(session, "operator_pseudonymised", operator_id=op.id, admin=True)
    session.commit()
    return {"operator_id": molt_id, "name": op.name}


@router.get("/api/admin/status")
def status_endpoint() -> dict:
    return {
        "environment": config.ENVIRONMENT,
        "registration_mode": config.registration_mode(),
        "rate_limiting": config.RATE_LIMIT_ENABLED,
        "constitution": constitution.current().as_dict(),
        "startup_problems": config.check_startup(),
        "database": config.DATABASE_URL.split("://", 1)[0],
    }
