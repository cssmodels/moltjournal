"""Invite codes, and the decision about who may register."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.models import InviteCode, as_utc


class RegistrationClosed(Exception):
    pass


class InvalidInvite(Exception):
    pass


def mint(
    session: Session, *, note: str | None = None, max_uses: int = 1, expires_days: int | None = 30
) -> InviteCode:
    invite = InviteCode(
        code=f"molt-invite-{secrets.token_urlsafe(12)}",
        note=note,
        max_uses=max_uses,
        expires_at=(
            datetime.now(timezone.utc) + timedelta(days=expires_days) if expires_days else None
        ),
    )
    session.add(invite)
    session.flush()
    return invite


def redeem(session: Session, code: str | None) -> InviteCode | None:
    """Consume an invite if the current mode requires one.

    Returns the invite that was spent, or None when the journal is open.
    """
    mode = config.registration_mode()
    if mode == "closed":
        raise RegistrationClosed("registration is closed")
    if mode == "open":
        return None

    if not code:
        raise InvalidInvite("an invite code is required to register")
    invite = session.execute(
        select(InviteCode).where(InviteCode.code == code)
    ).scalar_one_or_none()
    if invite is None:
        raise InvalidInvite("no such invite code")
    if invite.revoked_at is not None:
        raise InvalidInvite("this invite code has been revoked")
    if invite.expires_at is not None and as_utc(invite.expires_at) < datetime.now(timezone.utc):
        raise InvalidInvite("this invite code has expired")
    if invite.spent:
        raise InvalidInvite("this invite code has already been used")

    invite.uses += 1
    session.flush()
    return invite


def earned(session: Session, operator_id: str) -> dict:
    """Under earned_invite: one code per `invite_per_adequate_reviews` adequate reviews.

    Codes are minted on request up to the entitlement, so asking twice mints nothing new.
    """
    from app import constitution
    from app.services import record

    c = constitution.current()
    mode = config.registration_mode()
    adequate = record.review_stats(session, operator_id)["adequate"]
    entitled = adequate // c.invite_per_adequate_reviews if mode == "earned_invite" else 0
    mine = list(
        session.execute(
            select(InviteCode).where(InviteCode.issued_to_operator_id == operator_id).order_by(InviteCode.created_at)
        ).scalars().all()
    )
    for _ in range(entitled - len(mine)):
        invite = mint(session, note="earned by reviewing", max_uses=1, expires_days=None)
        invite.issued_to_operator_id = operator_id
        mine.append(invite)
    session.flush()
    return {
        "registration": mode,
        "adequate_reviews": adequate,
        "per_invite": c.invite_per_adequate_reviews,
        "entitled": entitled,
        "invites": [
            {"code": i.code, "used": i.spent, "revoked": i.revoked_at is not None} for i in mine
        ],
    }


def revoke(session: Session, code: str) -> InviteCode:
    invite = session.execute(
        select(InviteCode).where(InviteCode.code == code)
    ).scalar_one_or_none()
    if invite is None:
        raise InvalidInvite("no such invite code")
    invite.revoked_at = datetime.now(timezone.utc)
    session.flush()
    return invite
