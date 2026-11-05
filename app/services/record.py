"""An operator's activity record, and editorial eligibility derived from it.

There is deliberately no score. The record is a list of things that happened;
thresholds are computed from it internally and only ever answer one yes/no
question: may this operator edit in this field?
"""

from __future__ import annotations

import json
from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import constitution
from app.models import (
    Evaluation,
    Manuscript,
    ManuscriptVersion,
    Operator,
    Review,
    State,
)


def review_stats(session: Session, operator_id: str) -> dict:
    rows = session.execute(
        select(Review.evaluation, func.count(Review.id))
        .where(Review.reviewer_operator_id == operator_id)
        .group_by(Review.evaluation)
    ).all()
    counts = {evaluation: n for evaluation, n in rows}
    adequate = counts.get(Evaluation.ADEQUATE, 0)
    inadequate = counts.get(Evaluation.INADEQUATE, 0)
    awaiting = counts.get(None, 0)
    evaluated = adequate + inadequate
    return {
        "completed": adequate + inadequate + awaiting,
        "adequate": adequate,
        "inadequate": inadequate,
        "awaiting_evaluation": awaiting,
        "adequacy_rate": round(adequate / evaluated, 3) if evaluated else None,
    }


def field_stats(session: Session, operator_id: str) -> dict[str, dict]:
    """Evaluated reviews by the primary field of the paper reviewed."""
    rows = session.execute(
        select(ManuscriptVersion.primary_field, Review.evaluation, func.count(Review.id))
        .join(ManuscriptVersion, ManuscriptVersion.id == Review.version_id)
        .where(Review.reviewer_operator_id == operator_id, Review.evaluation.is_not(None))
        .group_by(ManuscriptVersion.primary_field, Review.evaluation)
    ).all()
    tally: dict[str, dict] = defaultdict(lambda: {"adequate": 0, "inadequate": 0})
    for field, evaluation, n in rows:
        tally[field][evaluation] = n
    out = {}
    for field, t in tally.items():
        evaluated = t["adequate"] + t["inadequate"]
        out[field] = {
            "adequate": t["adequate"],
            "inadequate": t["inadequate"],
            "adequacy_rate": round(t["adequate"] / evaluated, 3) if evaluated else None,
        }
    return out


def editor_fields(session: Session, operator: Operator) -> list[str]:
    """Fields in which this operator's review record makes it an editor."""
    c = constitution.current()
    eligible = []
    for field, stats in field_stats(session, operator.id).items():
        rate = stats["adequacy_rate"]
        if (
            stats["adequate"] >= c.editor_min_adequate_reviews
            and rate is not None
            and rate >= c.editor_min_adequacy_rate
        ):
            eligible.append(field)
    return sorted(eligible)


def can_edit(session: Session, operator: Operator, fields: set[str]) -> bool:
    if not appeals_eligible(session, operator.id):
        return False
    if constitution.current().editor_selection == "random":
        return True
    return bool(set(editor_fields(session, operator)) & fields)


def appeal_stats(session: Session, operator_id: str) -> dict:
    """How this operator's judgements have fared on appeal."""
    from app.models import Appeal, AppealState

    c = constitution.current()
    rows = session.execute(
        select(Appeal.state, func.count(Appeal.id))
        .where(Appeal.original_editor_operator_id == operator_id)
        .group_by(Appeal.state)
    ).all()
    by_state = {state: n for state, n in rows}
    overturned = by_state.get(AppealState.OVERTURNED, 0)
    ruled = overturned + by_state.get(AppealState.UPHELD, 0)
    rate = round(overturned / ruled, 3) if ruled else None
    return {
        "appealed": sum(by_state.values()),
        "ruled": ruled,
        "overturned": overturned,
        "overturn_rate": rate,
        "editor_eligible": not (
            ruled >= c.editor_overturn_min_appeals and rate is not None and rate > c.editor_max_overturn_rate
        ),
    }


def appeals_eligible(session: Session, operator_id: str) -> bool:
    return appeal_stats(session, operator_id)["editor_eligible"]


def correctness(session: Session, operator_id: str) -> dict:
    """Did this operator's recommendations agree with how the papers turned out?

    Display only: it never affects eligibility. A lenient call is accept or minor
    revision on a paper later CORRECTED or WITHDRAWN; a harsh call is reject or major
    revision on a paper that was later accepted.
    """
    from app.models import PaperStatus, Recommendation, ReviewKind

    rows = session.execute(
        select(Review.soundness_recommendation, Manuscript.state, Manuscript.current_status)
        .join(Manuscript, Manuscript.id == Review.manuscript_id)
        .where(Review.reviewer_operator_id == operator_id, Review.kind == ReviewKind.PRE)
    ).all()
    lenient = harsh = 0
    for recommendation, state, status in rows:
        if recommendation in (Recommendation.ACCEPT, Recommendation.MINOR_REVISION) and status in (
            PaperStatus.CORRECTED,
            PaperStatus.WITHDRAWN,
        ):
            lenient += 1
        elif recommendation in (Recommendation.REJECT, Recommendation.MAJOR_REVISION) and state == State.PUBLISHED:
            harsh += 1
    return {
        "reviews": len(rows),
        "accepting_on_later_corrected_or_withdrawn": lenient,
        "rejecting_on_later_accepted": harsh,
        "note": "Shown for information. It does not affect eligibility.",
    }


def lapses(session: Session, operator_id: str) -> dict:
    from datetime import timedelta

    from app.models import Event, as_utc, utcnow

    c = constitution.current()
    since = utcnow() - timedelta(days=c.lapse_window_days)
    stamps = session.execute(
        select(Event.created_at).where(Event.kind == "lapse", Event.operator_id == operator_id)
    ).scalars().all()
    return {"total": len(stamps), "recent": sum(1 for t in stamps if as_utc(t) >= since)}


def models_used(session: Session, operator_id: str) -> list[dict]:
    """Models this operator has reviewed with, then any it has declared but not yet used."""
    from app.models import Agent

    seen: dict[tuple, int] = defaultdict(int)
    for raw in session.execute(
        select(Review.model_provenance_json).where(Review.reviewer_operator_id == operator_id)
    ).scalars():
        p = json.loads(raw)
        seen[(p["provider"], p["model_family"], p["model_version"])] += 1
    for agent in session.execute(select(Agent).where(Agent.operator_id == operator_id)).scalars():
        seen.setdefault((agent.provider, agent.model_family, agent.model_version), 0)
    return [
        {"provider": a, "model_family": b, "model_version": c, "reviews": n}
        for (a, b, c), n in sorted(seen.items(), key=lambda kv: -kv[1])
    ]


def manuscript_counts(session: Session, operator_id: str) -> dict:
    rows = session.execute(
        select(Manuscript.state, func.count(Manuscript.id))
        .where(Manuscript.operator_id == operator_id)
        .group_by(Manuscript.state)
    ).all()
    by_state = {state: n for state, n in rows}
    return {
        "submitted": sum(by_state.values()),
        "accepted": by_state.get(State.PUBLISHED, 0),
        "rejected": by_state.get(State.REJECTED, 0),
        "withdrawn": by_state.get(State.WITHDRAWN, 0),
        "in_progress": sum(
            n
            for state, n in by_state.items()
            if state not in State.TERMINAL
        ),
    }
