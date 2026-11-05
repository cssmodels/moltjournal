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
    mode = config.REGISTRATION_MODE
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


def revoke(session: Session, code: str) -> InviteCode:
    invite = session.execute(
        select(InviteCode).where(InviteCode.code == code)
    ).scalar_one_or_none()
    if invite is None:
        raise InvalidInvite("no such invite code")
    invite.revoked_at = datetime.now(timezone.utc)
    session.flush()
    return invite
