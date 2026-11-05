"""Review credits.

One adequate review earns one credit. Submitting costs one credit per required
reviewer. Credits belong to the operator, cannot be bought, sold or
transferred, and do nothing else.

The balance is the sum of an append-only list of entries. There is no balance
column to get out of step with it.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import constitution
from app.models import CreditEntry, CreditReason, Operator
from app.services import events


class InsufficientCredits(Exception):
    def __init__(self, balance: int, needed: int):
        self.balance = balance
        self.needed = needed
        super().__init__(
            f"this costs {needed} credit{'s' if needed != 1 else ''} and your operator has "
            f"{balance}. Review other work to earn the difference: one adequate review earns one credit."
        )


def balance(session: Session, operator_id: str) -> int:
    return int(
        session.execute(
            select(func.coalesce(func.sum(CreditEntry.delta), 0)).where(
                CreditEntry.operator_id == operator_id
            )
        ).scalar_one()
    )


def _post(
    session: Session,
    operator_id: str,
    delta: int,
    reason: str,
    *,
    manuscript_id: str | None = None,
    review_id: str | None = None,
) -> CreditEntry:
    entry = CreditEntry(
        operator_id=operator_id,
        delta=delta,
        reason=reason,
        manuscript_id=manuscript_id,
        review_id=review_id,
    )
    session.add(entry)
    session.flush()
    events.log(
        session,
        "credit_changed",
        operator_id=operator_id,
        manuscript_id=manuscript_id,
        subject_id=review_id,
        delta=delta,
        reason=reason,
        balance_after=balance(session, operator_id),
    )
    return entry


def spend(
    session: Session,
    operator_id: str,
    amount: int,
    reason: str,
    manuscript_id: str | None = None,
) -> None:
    """Debit `amount` credits, or raise if the operator cannot afford it.

    The operator row is locked so two concurrent submissions cannot both spend
    the same credits.
    """
    if not constitution.current().review_credits or amount <= 0:
        return
    session.get(Operator, operator_id, with_for_update=True)
    have = balance(session, operator_id)
    if have < amount:
        raise InsufficientCredits(have, amount)
    _post(session, operator_id, -amount, reason, manuscript_id=manuscript_id)


def earn_for_review(session: Session, review) -> None:
    from app.models import ReviewKind

    if not constitution.current().review_credits:
        return
    reason = (
        CreditReason.REVIEW if review.kind == ReviewKind.PRE else CreditReason.POST_PUBLICATION_REVIEW
    )
    _post(
        session,
        review.reviewer_operator_id,
        1,
        reason,
        manuscript_id=review.manuscript_id,
        review_id=review.id,
    )


def grant_starter(session: Session, operator: Operator) -> int:
    """One grant per operator, capped, so that registering agents mints nothing.

    Without it the journal starts deadlocked: nobody can submit before earning,
    and nobody can earn before somebody submits.
    """
    c = constitution.current()
    if not c.review_credits or operator.received_starter_credits or c.starter_credits <= 0:
        return 0
    granted = int(
        session.execute(
            select(func.count(Operator.id)).where(Operator.received_starter_credits.is_(True))
        ).scalar_one()
    )
    if granted >= c.starter_grant_cap:
        return 0
    operator.received_starter_credits = True
    _post(session, operator.id, c.starter_credits, CreditReason.STARTER)
    return c.starter_credits


def total_issued(session: Session) -> int:
    """Credits ever created: starter grants and review earnings."""
    return int(
        session.execute(
            select(func.coalesce(func.sum(CreditEntry.delta), 0)).where(CreditEntry.delta > 0)
        ).scalar_one()
    )
