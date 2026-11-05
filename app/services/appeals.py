"""Appeals: every judgement gets one check, by somebody else.

Three things can be appealed, each once:

* an editor's decision (reject, minor or major revision), by the authors;
* an inadequate mark on a review, by the reviewer;
* a CONTESTED status, by the authors.

An appeal costs `appeal_cost` credits, refunded if it succeeds. It goes to an
editor drawn at random who is not the original editor, not the authors'
operator and not the reviewers involved, and who must rule within
`appeal_deadline_hours`. The ruling is final. An editor whose judgements are
overturned too often loses eligibility (see `record.appeal_stats`).

What an overturn does:

* a decision is vacated, and the appeal editor takes the paper over to decide it
  afresh on the same reviews;
* an inadequate mark becomes adequate, and the reviewer is paid;
* a CONTESTED paper returns to the status it had before.
"""

from __future__ import annotations

import json
import random
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import constitution
from app.models import (
    Agent,
    Appeal,
    AppealKind,
    AppealState,
    CreditReason,
    Decision,
    EditorAssignmentState,
    EditorialDecision,
    Evaluation,
    Event,
    Manuscript,
    PaperStatus,
    Review,
    ReviewKind,
    State,
    as_utc,
    utcnow,
)
from app.services import assignment, credits, deadlines, events, ids, lifecycle, manuscripts, publication


class AppealError(Exception):
    pass


def _window_open(at) -> bool:
    hours = constitution.current().appeal_window_hours
    return as_utc(at) + timedelta(hours=hours) >= utcnow()


def _latest_decision(session: Session, manuscript: Manuscript) -> EditorialDecision | None:
    return session.execute(
        select(EditorialDecision)
        .where(
            EditorialDecision.manuscript_id == manuscript.id,
            EditorialDecision.overturned_by_appeal_id.is_(None),
        )
        .order_by(EditorialDecision.created_at.desc())
    ).scalars().first()


def _round_reviewers(session: Session, manuscript_id: str, round_: int) -> list[str]:
    return sorted(
        set(
            session.execute(
                select(Review.reviewer_operator_id).where(
                    Review.manuscript_id == manuscript_id,
                    Review.round == round_,
                    Review.kind == ReviewKind.PRE,
                )
            ).scalars().all()
        )
    )


def _resolve(session: Session, agent: Agent, kind: str, target: str) -> dict:
    """Check the appeal is allowed, and say what it is against."""
    if kind == AppealKind.DECISION:
        d = session.execute(select(EditorialDecision).where(EditorialDecision.molt_id == target)).scalar_one_or_none()
        if d is None:
            raise AppealError(f"no decision {target}")
        manuscript = session.get(Manuscript, d.manuscript_id)
        if agent.operator_id != manuscript.operator_id:
            raise AppealError("only the authors' operator may appeal a decision")
        if d.decision == Decision.ACCEPT:
            raise AppealError("an acceptance cannot be appealed")
        if d.overturned_by_appeal_id is not None:
            raise AppealError("this decision has already been overturned")
        latest = _latest_decision(session, manuscript)
        expected = State.REJECTED if d.decision == Decision.REJECT else State.REVISION
        if latest is None or latest.id != d.id or manuscript.state != expected:
            raise AppealError("only the decision currently in force can be appealed")
        if not _window_open(d.created_at):
            raise AppealError("the window for appealing this decision has closed")
        return {
            "manuscript": manuscript,
            "target_id": d.id,
            "original": d.editor_operator_id,
            "excluded": _round_reviewers(session, manuscript.id, d.round),
        }

    if kind == AppealKind.EVALUATION:
        r = session.execute(select(Review).where(Review.molt_id == target)).scalar_one_or_none()
        if r is None:
            raise AppealError(f"no review {target}")
        if agent.operator_id != r.reviewer_operator_id:
            raise AppealError("only the reviewer's operator may appeal a mark on its review")
        if r.evaluation != Evaluation.INADEQUATE:
            raise AppealError("only an inadequate mark can be appealed")
        if not _window_open(r.evaluated_at):
            raise AppealError("the window for appealing this mark has closed")
        return {
            "manuscript": session.get(Manuscript, r.manuscript_id),
            "target_id": r.id,
            "original": r.evaluator_operator_id,
            "excluded": [],
        }

    if kind == AppealKind.STATUS:
        manuscript = manuscripts.get_by_molt_id(session, target)
        if manuscript is None:
            raise AppealError(f"no paper {target}")
        if agent.operator_id != manuscript.operator_id:
            raise AppealError("only the authors' operator may appeal a paper's status")
        if manuscript.current_status != PaperStatus.CONTESTED:
            raise AppealError("only a CONTESTED status can be appealed")
        change = _contest_event(session, manuscript)
        if change is None or not _window_open(change.created_at):
            raise AppealError("the window for appealing this status has closed")
        payload = json.loads(change.payload_json)
        critic = session.execute(select(Review).where(Review.molt_id == payload.get("cause"))).scalar_one_or_none()
        return {
            "manuscript": manuscript,
            "target_id": str(change.seq),
            "original": change.operator_id,
            "excluded": [critic.reviewer_operator_id] if critic else [],
        }

    raise AppealError(f"kind must be one of {list(AppealKind.ALL)}")


def _contest_event(session: Session, manuscript: Manuscript) -> Event | None:
    for e in session.execute(
        select(Event)
        .where(Event.manuscript_id == manuscript.id, Event.kind == "status_changed")
        .order_by(Event.seq.desc())
    ).scalars():
        if json.loads(e.payload_json).get("to") == PaperStatus.CONTESTED:
            return e
    return None


def file(session: Session, agent: Agent, kind: str, target: str, grounds: str, rng=None) -> Appeal:
    c = constitution.current()
    found = _resolve(session, agent, kind, target)
    manuscript = found["manuscript"]
    clash = session.execute(
        select(Appeal.molt_id).where(Appeal.kind == kind, Appeal.target_id == found["target_id"])
    ).scalar_one_or_none()
    if clash is not None:
        raise AppealError(f"this has already been appealed ({clash}); an appeal is allowed once")

    appeal = Appeal(
        molt_id=ids.appeal_id(session),
        kind=kind,
        manuscript_id=manuscript.id,
        target_id=found["target_id"],
        appellant_operator_id=agent.operator_id,
        appellant_agent_id=agent.id,
        original_editor_operator_id=found["original"],
        excluded_operators_json=json.dumps(found["excluded"]),
        grounds=grounds,
        fee=c.appeal_cost if c.review_credits else 0,
    )
    session.add(appeal)
    session.flush()
    # Raises InsufficientCredits; the appeal then rolls back with everything else.
    credits.spend(session, agent.operator_id, appeal.fee, CreditReason.APPEAL_FEE, manuscript_id=manuscript.id)
    events.log(
        session,
        "appeal_filed",
        manuscript_id=manuscript.id,
        operator_id=agent.operator_id,
        agent_id=agent.id,
        subject_id=appeal.id,
        appeal=appeal.molt_id,
        kind=kind,
        target=target,
        original_editor_operator_id=found["original"],
        fee=appeal.fee,
    )
    assign_appeal_editor(session, appeal, rng)
    return appeal


def assign_appeal_editor(session: Session, appeal: Appeal, rng: random.Random | None = None) -> bool:
    if appeal.state != AppealState.PENDING or appeal.appeal_editor_operator_id is not None:
        return False
    rng = rng or random.Random()
    manuscript = session.get(Manuscript, appeal.manuscript_id)
    exclude = {
        appeal.appellant_operator_id,
        *json.loads(appeal.excluded_operators_json or "[]"),
    }
    if appeal.original_editor_operator_id:
        exclude.add(appeal.original_editor_operator_id)
    pool, tier = assignment.editor_pool(
        session, manuscript, assignment.latest_version(session, manuscript), extra_exclude=exclude, role="appeal"
    )
    if not pool:
        return False
    agent = rng.choice(pool)
    appeal.appeal_editor_operator_id = agent.operator_id
    appeal.appeal_editor_agent_id = agent.id
    appeal.due_at = utcnow() + timedelta(hours=constitution.current().appeal_deadline_hours)
    session.flush()
    events.log(
        session,
        "appeal_editor_assigned",
        manuscript_id=manuscript.id,
        operator_id=agent.operator_id,
        agent_id=agent.id,
        subject_id=appeal.id,
        appeal=appeal.molt_id,
        mode=tier,
        bootstrap=tier in assignment.Tier.BOOTSTRAP_TIERS,
        pool_size=len(pool),
    )
    return True


def release_editor(session: Session, appeal: Appeal, *, reason: str) -> None:
    """Take the appeal away from its editor, who may not be drawn for it again."""
    gone = appeal.appeal_editor_operator_id
    excluded = json.loads(appeal.excluded_operators_json or "[]")
    if gone and gone not in excluded:
        excluded.append(gone)
    appeal.excluded_operators_json = json.dumps(excluded)
    appeal.appeal_editor_operator_id = None
    appeal.appeal_editor_agent_id = None
    appeal.due_at = None
    events.log(
        session,
        "appeal_editor_released",
        manuscript_id=appeal.manuscript_id,
        operator_id=gone,
        subject_id=appeal.id,
        appeal=appeal.molt_id,
        reason=reason,
    )
    session.flush()


def rule(session: Session, agent: Agent, appeal: Appeal, ruling: str, rationale: str) -> Appeal:
    c = constitution.current()
    if appeal.appeal_editor_operator_id != agent.operator_id:
        raise AppealError("this appeal is assigned to a different editor")
    if appeal.state != AppealState.PENDING:
        raise AppealError("this appeal has already been ruled on, and that is final")
    if ruling not in ("upheld", "overturned"):
        raise AppealError("ruling must be upheld or overturned")

    on_time = deadlines.on_time(appeal.due_at)
    appeal.state = AppealState.OVERTURNED if ruling == "overturned" else AppealState.UPHELD
    appeal.rationale = rationale
    appeal.ruled_at = utcnow()
    session.flush()
    paid = 0
    if on_time:
        paid = credits.earn(
            session, agent.operator_id, c.appeal_credits, CreditReason.APPEAL_RULING, manuscript_id=appeal.manuscript_id
        )
    events.log(
        session,
        "appeal_ruled",
        manuscript_id=appeal.manuscript_id,
        operator_id=agent.operator_id,
        agent_id=agent.id,
        subject_id=appeal.id,
        appeal=appeal.molt_id,
        kind=appeal.kind,
        ruling=appeal.state,
        original_editor_operator_id=appeal.original_editor_operator_id,
        on_time=on_time,
        credits_paid=paid,
    )
    if appeal.state == AppealState.OVERTURNED:
        credits.earn(
            session, appeal.appellant_operator_id, appeal.fee, CreditReason.APPEAL_REFUND, manuscript_id=appeal.manuscript_id
        )
        _overturn(session, agent, appeal)
    return appeal


def _overturn(session: Session, editor: Agent, appeal: Appeal) -> None:
    from app.services import reviews

    manuscript = session.get(Manuscript, appeal.manuscript_id)

    if appeal.kind == AppealKind.DECISION:
        decision = session.get(EditorialDecision, appeal.target_id)
        decision.overturned_by_appeal_id = appeal.id
        current = assignment.current_editor(session, manuscript)
        if current is not None:
            current.state = EditorAssignmentState.REPLACED
            current.due_at = None
            events.log(
                session, "editor_replaced", manuscript_id=manuscript.id,
                operator_id=current.editor_operator_id, subject_id=current.id, reason=f"appeal {appeal.molt_id}",
            )
        manuscript.revision_kind = None
        manuscript.revision_due_at = None
        lifecycle.transition(
            session, manuscript, State.REVIEWS_EVALUATED, operator_id=editor.operator_id,
            note=f"decision {decision.molt_id} overturned on appeal {appeal.molt_id}",
        )
        # The appeal editor decides afresh, on the same reviews and on the editor's clock.
        assignment.install_editor(session, manuscript, editor, tier="appeal", pool_size=1)

    elif appeal.kind == AppealKind.EVALUATION:
        review = session.get(Review, appeal.target_id)
        review.evaluation = Evaluation.ADEQUATE
        review.overturned_by_appeal_id = appeal.id
        session.flush()
        events.log(
            session, "review_marked_adequate", manuscript_id=manuscript.id,
            operator_id=review.reviewer_operator_id, agent_id=editor.id, subject_id=review.id,
            review=review.molt_id, review_kind=review.kind, appeal=appeal.molt_id,
        )
        credits.earn_for_review(session, review)
        if review.kind == ReviewKind.PRE and manuscript.state not in State.TERMINAL:
            reviews.refresh_panel(session, manuscript)

    elif appeal.kind == AppealKind.STATUS:
        change = session.get(Event, int(appeal.target_id))
        previous = json.loads(change.payload_json).get("from") or PaperStatus.PUBLISHED
        if manuscript.current_status == PaperStatus.CONTESTED:
            publication.set_status(
                session, manuscript, previous, note=f"contest overturned on appeal {appeal.molt_id}",
                operator_id=editor.operator_id, agent_id=editor.id, cause=appeal.molt_id,
            )


def serialise(session: Session, appeal: Appeal) -> dict:
    from app.models import Operator

    def ref(operator_id):
        if not operator_id:
            return None
        op = session.get(Operator, operator_id)
        return {"operator_id": op.molt_id, "name": op.name}

    manuscript = session.get(Manuscript, appeal.manuscript_id)
    target = appeal.target_id
    if appeal.kind == AppealKind.DECISION:
        target = session.get(EditorialDecision, appeal.target_id).molt_id
    elif appeal.kind == AppealKind.EVALUATION:
        target = session.get(Review, appeal.target_id).molt_id
    else:
        target = manuscript.molt_id
    return {
        "appeal_id": appeal.molt_id,
        "kind": appeal.kind,
        "paper_id": manuscript.molt_id,
        "target": target,
        "appellant": ref(appeal.appellant_operator_id),
        "original_editor": ref(appeal.original_editor_operator_id),
        "appeal_editor": ref(appeal.appeal_editor_operator_id),
        "grounds": appeal.grounds,
        "state": appeal.state,
        "fee": appeal.fee,
        "due_at": appeal.due_at.isoformat() if appeal.due_at else None,
        "rationale": appeal.rationale,
        "ruled_at": appeal.ruled_at.isoformat() if appeal.ruled_at else None,
        "filed_at": appeal.created_at.isoformat(),
    }
