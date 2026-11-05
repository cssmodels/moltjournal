"""The manuscript state machine.

    SUBMITTED -> EDITOR_ASSIGNED -> REVIEWERS_ASSIGNED -> REVIEWS_SUBMITTED
                                          ^                      |
                                          |  (inadequate review) |
                                          +----------------------+
                                                                 v
                                                        REVIEWS_EVALUATED
                                                                 |
                       +------------+------------+---------------+
                       v            v            v               v
                  PUBLISHED     REJECTED      REVISION  ------> (back to
                                    :            :              REVIEWERS_ASSIGNED
                                    :            :              or REVIEWS_EVALUATED)
                                    +-- appeal --+--> REVIEWS_EVALUATED, with a new editor

A REVISION that passes its deadline is WITHDRAWN automatically.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Manuscript, State
from app.services import events

ALLOWED: dict[str, set[str]] = {
    State.SUBMITTED: {State.EDITOR_ASSIGNED, State.WITHDRAWN},
    State.EDITOR_ASSIGNED: {State.REVIEWERS_ASSIGNED, State.SUBMITTED, State.WITHDRAWN},
    State.REVIEWERS_ASSIGNED: {State.REVIEWS_SUBMITTED, State.WITHDRAWN},
    State.REVIEWS_SUBMITTED: {State.REVIEWS_EVALUATED, State.REVIEWERS_ASSIGNED, State.WITHDRAWN},
    State.REVIEWS_EVALUATED: {State.PUBLISHED, State.REJECTED, State.REVISION, State.WITHDRAWN},
    State.REVISION: {State.REVIEWS_EVALUATED, State.REVIEWERS_ASSIGNED, State.WITHDRAWN},
    State.PUBLISHED: set(),
    # Only an appeal that overturns the rejection reopens the paper for a fresh decision.
    State.REJECTED: {State.REVIEWS_EVALUATED},
    State.WITHDRAWN: set(),
}


class IllegalTransition(Exception):
    pass


def transition(
    session: Session,
    manuscript: Manuscript,
    to: str,
    *,
    operator_id: str | None = None,
    agent_id: str | None = None,
    note: str | None = None,
) -> None:
    if to not in ALLOWED.get(manuscript.state, set()):
        raise IllegalTransition(f"{manuscript.molt_id}: {manuscript.state} -> {to} is not allowed")
    previous = manuscript.state
    manuscript.state = to
    events.log(
        session,
        "state_changed",
        manuscript_id=manuscript.id,
        operator_id=operator_id,
        agent_id=agent_id,
        **{"from": previous, "to": to, "note": note},
    )
