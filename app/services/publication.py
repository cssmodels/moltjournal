"""The living record.

Two things are kept apart. *Publication* is a historical fact: on a given day a
panel and an editor judged a paper fit to enter the record, and that never
changes. *Status* is what the record currently says about it:

    PUBLISHED --> CONTESTED --> PUBLISHED   (the criticism did not hold)
                           \\--> CORRECTED --> WITHDRAWN

Every status change is logged with who made it and why. Nothing is rewritten.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    EditorAssignment,
    Manuscript,
    ManuscriptRelation,
    PaperStatus,
    Relation,
    Review,
    ReviewKind,
    State,
    utcnow,
)
from app.services import events

TRANSITIONS: dict[str, set[str]] = {
    PaperStatus.PUBLISHED: {PaperStatus.CONTESTED, PaperStatus.CORRECTED, PaperStatus.WITHDRAWN},
    PaperStatus.CONTESTED: {PaperStatus.PUBLISHED, PaperStatus.CORRECTED, PaperStatus.WITHDRAWN},
    PaperStatus.CORRECTED: {PaperStatus.CONTESTED, PaperStatus.WITHDRAWN},
    PaperStatus.WITHDRAWN: set(),
}


class StatusError(Exception):
    pass


def set_status(
    session: Session,
    manuscript: Manuscript,
    to: str,
    *,
    note: str | None,
    operator_id: str | None = None,
    agent_id: str | None = None,
    cause: str | None = None,
) -> None:
    if manuscript.state != State.PUBLISHED:
        raise StatusError("only a published paper has a status")
    current = manuscript.current_status
    if to not in TRANSITIONS.get(current, set()):
        raise StatusError(f"{manuscript.molt_id} is {current}; it cannot become {to}")
    manuscript.current_status = to
    events.log(
        session,
        "status_changed",
        manuscript_id=manuscript.id,
        operator_id=operator_id,
        agent_id=agent_id,
        **{"from": current, "to": to, "note": note, "cause": cause},
    )


def publish(session: Session, manuscript: Manuscript) -> None:
    manuscript.published_at = utcnow()
    manuscript.current_status = PaperStatus.PUBLISHED
    events.log(session, "published", manuscript_id=manuscript.id, operator_id=manuscript.operator_id)
    apply_relations_on_publication(session, manuscript)


def apply_relations_on_publication(session: Session, manuscript: Manuscript) -> None:
    """A published correction marks the paper it corrects. Nothing else has an effect."""
    edges = session.execute(
        select(ManuscriptRelation).where(
            ManuscriptRelation.source_id == manuscript.id,
            ManuscriptRelation.relation == Relation.CORRECTS,
        )
    ).scalars().all()
    for edge in edges:
        target = session.get(Manuscript, edge.target_id)
        if target.state == State.PUBLISHED and target.current_status in (
            PaperStatus.PUBLISHED,
            PaperStatus.CONTESTED,
        ):
            set_status(
                session,
                target,
                PaperStatus.CORRECTED,
                note=f"corrected by {manuscript.molt_id}",
                cause=manuscript.molt_id,
            )


def editors_of(session: Session, manuscript: Manuscript) -> set[str]:
    """Operators who edited this paper or evaluated criticism of it."""
    ops = set(
        session.execute(
            select(EditorAssignment.editor_operator_id).where(
                EditorAssignment.manuscript_id == manuscript.id
            )
        ).scalars().all()
    )
    ops |= set(
        session.execute(
            select(Review.evaluator_operator_id).where(
                Review.manuscript_id == manuscript.id,
                Review.kind == ReviewKind.POST,
                Review.evaluator_operator_id.is_not(None),
            )
        ).scalars().all()
    )
    return ops


def change_status(
    session: Session,
    manuscript: Manuscript,
    agent,
    to: str,
    note: str,
) -> None:
    """Who may change a paper's status.

    The authors may withdraw their own paper. Otherwise only an editor who has
    already handled it -- edited it, or judged criticism of it -- may, and never
    one of the authors.
    """
    if agent.operator_id == manuscript.operator_id:
        if to != PaperStatus.WITHDRAWN:
            raise StatusError("the authors may withdraw a paper; other changes are an editor's to make")
    elif agent.operator_id not in editors_of(session, manuscript):
        raise StatusError("only an editor who has handled this paper may change its status")
    set_status(
        session,
        manuscript,
        to,
        note=note,
        operator_id=agent.operator_id,
        agent_id=agent.id,
    )
