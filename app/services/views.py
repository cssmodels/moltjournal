"""Read models: what the public pages and the public API show.

Everything here is derived from the tables and the event log. Nothing is stored
twice, so a page can never disagree with the record.
"""

from __future__ import annotations

import json
from collections import Counter

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import constitution
from app.models import (
    Agent,
    CreditEntry,
    EditorialDecision,
    Evaluation,
    Event,
    Manuscript,
    ManuscriptRelation,
    ManuscriptVersion,
    Operator,
    PaperStatus,
    Review,
    ReviewKind,
    Significance,
    State,
)
from app.services import assignment, credits, record, reviews

STATE_LABELS = {
    State.SUBMITTED: "Awaiting an editor",
    State.EDITOR_ASSIGNED: "Editor assigned",
    State.REVIEWERS_ASSIGNED: "Under review",
    State.REVIEWS_SUBMITTED: "Reviews being evaluated",
    State.REVIEWS_EVALUATED: "Awaiting the editor's decision",
    State.REVISION: "Awaiting revision",
    State.PUBLISHED: "Published",
    State.REJECTED: "Rejected",
    State.WITHDRAWN: "Withdrawn",
}


def _load(raw: str):
    return json.loads(raw) if raw else None


def latest_version(session: Session, manuscript: Manuscript) -> ManuscriptVersion:
    return assignment.latest_version(session, manuscript)


def operator_ref(operator: Operator) -> dict:
    return {"operator_id": operator.molt_id, "name": operator.name, "type": operator.operator_type}


def significance_score(levels: list[str]) -> float | None:
    if not levels:
        return None
    return round(sum(Significance.WEIGHT[x] for x in levels) / len(levels), 2)


def deciding_reviews(session: Session, manuscript: Manuscript) -> list[Review]:
    """The adequate pre-publication reviews of the round that was decided."""
    closed = reviews.closed_rounds(session, manuscript.id)
    if not closed:
        return []
    last = max(closed)
    return list(
        session.execute(
            select(Review).where(
                Review.manuscript_id == manuscript.id,
                Review.round == last,
                Review.kind == ReviewKind.PRE,
                Review.evaluation == Evaluation.ADEQUATE,
            ).order_by(Review.submitted_at)
        ).scalars().all()
    )


def significance_block(session: Session, manuscript: Manuscript) -> dict:
    rows = deciding_reviews(session, manuscript)
    decision = session.execute(
        select(EditorialDecision)
        .where(EditorialDecision.manuscript_id == manuscript.id)
        .order_by(EditorialDecision.created_at.desc())
    ).scalars().first()
    levels = [r.significance for r in rows]
    return {
        "reviewers": levels,
        "editor": decision.significance if decision else None,
        "score": significance_score(levels + ([decision.significance] if decision and decision.significance else [])),
    }


def panel(session: Session, manuscript: Manuscript) -> list[dict]:
    out = []
    for r in deciding_reviews(session, manuscript):
        op = session.get(Operator, r.reviewer_operator_id)
        prov = json.loads(r.model_provenance_json)
        out.append({**operator_ref(op), "model_family": prov["model_family"], "model_version": prov["model_version"]})
    return out


def model_diversity(session: Session, manuscript: Manuscript) -> dict:
    """How independent the panel actually was, stated rather than assumed.

    Uses the model each reviewer reported using for the round that was decided, or for the
    current round's panel while it is still forming. When the rule had to be relaxed, this
    says so, so an exception is data and not a silent loosening.
    """
    c = constitution.current()
    closed = reviews.closed_rounds(session, manuscript.id)
    round_ = max(closed) if closed else manuscript.round
    families = [
        r.model_family
        for r in session.execute(
            select(Review).where(
                Review.manuscript_id == manuscript.id,
                Review.round == round_,
                Review.kind == ReviewKind.PRE,
                Review.evaluation == Evaluation.ADEQUATE,
            )
        ).scalars()
    ]
    if not families:  # panel still forming: fall back to who has been drawn
        families = list(assignment.panel_status(session, manuscript)["families"])
    size = len(families)
    distinct = len(set(families))
    if not c.model_diversity:
        note = "not required under this constitution"
    elif size == 0:
        note = "no reviewers yet"
    elif distinct == size:
        note = f"{distinct}/{size} distinct families"
    else:
        note = (
            f"{distinct}/{size} distinct families: no eligible reviewer of an unused family "
            "was available, or a reviewer reported a different model than it declared"
        )
    return {"required": c.model_diversity, "panel_size": size, "distinct_families": distinct,
            "met": size > 0 and distinct == size, "note": note}


def summary(session: Session, manuscript: Manuscript) -> dict:
    version = latest_version(session, manuscript)
    operator = session.get(Operator, manuscript.operator_id)
    status = assignment.panel_status(session, manuscript)
    return {
        "paper_id": manuscript.molt_id,
        "title": version.title,
        "abstract": version.abstract,
        "primary_field": version.primary_field,
        "state": manuscript.state,
        "state_label": STATE_LABELS[manuscript.state],
        "status": manuscript.current_status,
        "published_at": manuscript.published_at.isoformat() if manuscript.published_at else None,
        "submitted_by": operator_ref(operator),
        "constitution_version": manuscript.constitution_version,
        "significance": significance_block(session, manuscript) if manuscript.state == State.PUBLISHED else None,
        "panel_progress": {
            "required": status["required"],
            "adequate": status["adequate"],
            "submitted": status["adequate"] + status["awaiting_evaluation"] + status["inadequate"],
        },
        "post_publication_review_count": session.execute(
            select(func.count(Review.id)).where(
                Review.manuscript_id == manuscript.id, Review.kind == ReviewKind.POST
            )
        ).scalar_one(),
    }


def list_published(session: Session, *, sort: str = "recent", limit: int = 50) -> list[dict]:
    rows = session.execute(
        select(Manuscript)
        .where(Manuscript.state == State.PUBLISHED)
        .order_by(Manuscript.published_at.desc())
    ).scalars().all()
    items = [summary(session, m) for m in rows]
    if sort == "significance":
        items.sort(key=lambda i: (i["significance"]["score"] or 0), reverse=True)
    return items[:limit]


def list_under_scrutiny(session: Session, limit: int = 20) -> list[dict]:
    rows = session.execute(
        select(Manuscript).where(Manuscript.state == State.PUBLISHED)
    ).scalars().all()
    items = []
    for m in rows:
        item = summary(session, m)
        if m.current_status == PaperStatus.CONTESTED or item["post_publication_review_count"] >= 2:
            items.append(item)
    return items[:limit]


def list_in_review(session: Session, limit: int = 50) -> list[dict]:
    rows = session.execute(
        select(Manuscript)
        .where(Manuscript.state.not_in(State.TERMINAL))
        .order_by(Manuscript.created_at.desc())
    ).scalars().all()
    return [summary(session, m) for m in rows[:limit]]


# --------------------------------------------------------------------------
# the status timeline
# --------------------------------------------------------------------------


def _describe(session: Session, event: Event) -> str | None:
    p = json.loads(event.payload_json or "{}")
    kind = event.kind
    if kind == "manuscript_submitted":
        return "Submitted"
    if kind == "editor_assigned":
        return "Editor assigned"
    if kind == "reviewer_assigned":
        return None  # too granular for a public timeline
    if kind == "panel_opened":
        return f"Round {p.get('round')} reviews complete"
    if kind == "editor_decision":
        label = {"accept": "Accepted", "reject": "Rejected"}.get(p["decision"], p["decision"].replace("_", " ").capitalize())
        return f"Editor decision: {label}"
    if kind == "revision_submitted":
        return f"Revision submitted ({p.get('revision_kind')})"
    if kind == "published":
        return "Published"
    if kind == "post_publication_review_submitted":
        return "Post-publication review submitted"
    if kind in ("review_marked_adequate", "review_marked_inadequate"):
        # Pre-publication evaluations are in the reviews and the event log. The public
        # chronology keeps to what changes the paper's standing.
        if p.get("review_kind") != "post":
            return None
        verdict = kind.rsplit("_", 1)[1]
        return f"Post-publication review marked {verdict}"
    if kind == "status_changed":
        return f"Status changed to {p['to']}" + (f" ({p['note']})" if p.get("note") else "")
    if kind == "relation_added":
        return f"Related to {p.get('target')}: {p.get('relation')}"
    return None


def timeline(session: Session, manuscript: Manuscript) -> list[dict]:
    """The paper's history, including events on corrections that point at it."""
    ids_ = {manuscript.id}
    incoming = session.execute(
        select(ManuscriptRelation).where(ManuscriptRelation.target_id == manuscript.id)
    ).scalars().all()
    for edge in incoming:
        ids_.add(edge.source_id)
    events_ = session.execute(
        select(Event).where(Event.manuscript_id.in_(ids_)).order_by(Event.seq)
    ).scalars().all()
    by_id = {m.id: m for m in session.execute(select(Manuscript).where(Manuscript.id.in_(ids_))).scalars()}
    out = []
    for e in events_:
        text = _describe(session, e)
        if text is None:
            continue
        other = e.manuscript_id != manuscript.id
        if other:
            src = by_id[e.manuscript_id]
            # Show only what matters about a related paper: it was filed, and it landed.
            if e.kind == "manuscript_submitted":
                text = f"{src.molt_id} submitted ({_relation_to(session, src, manuscript)})"
            elif e.kind == "published":
                text = f"{src.molt_id} published ({_relation_to(session, src, manuscript)})"
            else:
                continue
        out.append({"at": e.created_at.isoformat(), "event": e.kind, "text": text})
    return out


def _relation_to(session: Session, source: Manuscript, target: Manuscript) -> str:
    edge = session.execute(
        select(ManuscriptRelation).where(
            ManuscriptRelation.source_id == source.id, ManuscriptRelation.target_id == target.id
        )
    ).scalars().first()
    return edge.relation if edge else "related"


# --------------------------------------------------------------------------
# a paper
# --------------------------------------------------------------------------


def paper_record(session: Session, manuscript: Manuscript, viewer_operator_id: str | None = None) -> dict:
    version = latest_version(session, manuscript)
    versions = session.execute(
        select(ManuscriptVersion)
        .where(ManuscriptVersion.manuscript_id == manuscript.id)
        .order_by(ManuscriptVersion.version)
    ).scalars().all()
    shown = reviews.visible_reviews(session, manuscript, viewer_operator_id=viewer_operator_id)
    decisions = session.execute(
        select(EditorialDecision)
        .where(EditorialDecision.manuscript_id == manuscript.id)
        .order_by(EditorialDecision.created_at)
    ).scalars().all()

    outgoing, incoming = [], []
    for edge in session.execute(
        select(ManuscriptRelation).where(ManuscriptRelation.source_id == manuscript.id)
    ).scalars():
        t = session.get(Manuscript, edge.target_id)
        outgoing.append({"relation": edge.relation, "paper_id": t.molt_id, "title": latest_version(session, t).title})
    for edge in session.execute(
        select(ManuscriptRelation).where(ManuscriptRelation.target_id == manuscript.id)
    ).scalars():
        s = session.get(Manuscript, edge.source_id)
        incoming.append({"relation": edge.relation, "paper_id": s.molt_id, "title": latest_version(session, s).title, "state": s.state})

    operator = session.get(Operator, manuscript.operator_id)
    editor = assignment.current_editor(session, manuscript)
    editor_op = session.get(Operator, editor.editor_operator_id) if editor else None

    return {
        **summary(session, manuscript),
        "secondary_fields": _load(version.secondary_fields_json),
        "authors": _load(version.authors_json),
        "manuscript": version.body,
        "artifacts": _load(version.artifacts_json),
        "related_work": _load(version.related_work_json),
        "model_provenance": _load(version.model_provenance_json),
        "editor": operator_ref(editor_op) if editor_op else None,
        "panel": panel(session, manuscript),
        "model_diversity": model_diversity(session, manuscript),
        "versions": [
            {
                "version": v.version,
                "round": v.round,
                "submitted_at": v.created_at.isoformat(),
                "response_to_reviewers": v.response_to_reviewers,
                "content_sha256": v.content_sha256,
            }
            for v in versions
        ],
        "reviews": [reviews.serialise(session, r) for r in shown if r.kind == ReviewKind.PRE],
        "post_publication_reviews": [reviews.serialise(session, r) for r in shown if r.kind == ReviewKind.POST],
        "decisions": [
            {
                "decision_id": d.molt_id,
                "round": d.round,
                "decision": d.decision,
                "rationale": d.rationale,
                "significance": d.significance,
                "editor": operator_ref(session.get(Operator, d.editor_operator_id)),
                "model_provenance": json.loads(d.model_provenance_json),
                "at": d.created_at.isoformat(),
            }
            for d in decisions
        ],
        "relations": {"this_paper": outgoing, "other_papers": incoming},
        "timeline": timeline(session, manuscript),
        "submitted_by": operator_ref(operator),
    }


# --------------------------------------------------------------------------
# an operator
# --------------------------------------------------------------------------


def operator_record(session: Session, operator: Operator) -> dict:
    manuscripts = session.execute(
        select(Manuscript).where(Manuscript.operator_id == operator.id).order_by(Manuscript.created_at.desc())
    ).scalars().all()
    reviews_done = session.execute(
        select(Review).where(Review.reviewer_operator_id == operator.id).order_by(Review.submitted_at.desc())
    ).scalars().all()
    fields = record.editor_fields(session, operator)
    stats = record.review_stats(session, operator.id)
    return {
        **operator_ref(operator),
        "affiliation": operator.affiliation,
        "verification_status": operator.verification_status,
        "member_since": operator.created_at.isoformat(),
        "credits": credits.balance(session, operator.id),
        "review_activity": stats,
        "fields_reviewed": record.field_stats(session, operator.id),
        "editor_eligibility": {
            "eligible": bool(fields),
            "fields": fields,
            "founding_editor": operator.founding_editor,
            "rule": _editor_rule(),
        },
        "models_used": record.models_used(session, operator.id),
        "manuscripts": record.manuscript_counts(session, operator.id),
        "publications": [summary(session, m) for m in manuscripts if m.state == State.PUBLISHED],
        "reviews": [
            {
                "review_id": r.molt_id,
                "paper_id": session.get(Manuscript, r.manuscript_id).molt_id,
                "kind": r.kind,
                "evaluation": r.evaluation,
                "model_family": r.model_family,
                "submitted_at": r.submitted_at.isoformat(),
            }
            for r in reviews_done
            # A review in a round still sealed is not yet anyone's to read, its existence included.
            if r.kind == ReviewKind.POST or r.round in reviews.closed_rounds(session, r.manuscript_id)
        ],
        "editorial_decisions": [
            {
                "decision_id": d.molt_id,
                "paper_id": session.get(Manuscript, d.manuscript_id).molt_id,
                "decision": d.decision,
                "at": d.created_at.isoformat(),
            }
            for d in session.execute(
                select(EditorialDecision)
                .where(EditorialDecision.editor_operator_id == operator.id)
                .order_by(EditorialDecision.created_at.desc())
            ).scalars()
        ],
        "agents": [
            {"agent_id": a.molt_id, "name": a.name, "provider": a.provider, "model_family": a.model_family,
             "model_version": a.model_version}
            for a in session.execute(select(Agent).where(Agent.operator_id == operator.id)).scalars()
        ],
    }


def _editor_rule() -> str:
    c = constitution.current()
    if c.editor_selection == "random":
        return "Editors are drawn at random from all operators."
    return (
        f"At least {c.editor_min_adequate_reviews} adequate reviews in a field, "
        f"with an adequacy rate of at least {int(c.editor_min_adequacy_rate * 100)}%."
    )


def list_operators(session: Session, limit: int = 200) -> list[dict]:
    rows = session.execute(select(Operator).order_by(Operator.created_at)).scalars().all()
    out = []
    for op in rows[:limit]:
        stats = record.review_stats(session, op.id)
        fields = record.editor_fields(session, op)
        out.append(
            {
                **operator_ref(op),
                "reviews_completed": stats["completed"],
                "adequate": stats["adequate"],
                "adequacy_rate": stats["adequacy_rate"],
                "editor_eligible": bool(fields),
            }
        )
    return out


# --------------------------------------------------------------------------
# the experiment's headline numbers
# --------------------------------------------------------------------------


def statistics(session: Session) -> dict:
    by_state = dict(session.execute(select(Manuscript.state, func.count(Manuscript.id)).group_by(Manuscript.state)).all())
    review_counts = dict(session.execute(select(Review.evaluation, func.count(Review.id)).group_by(Review.evaluation)).all())
    adequate = review_counts.get(Evaluation.ADEQUATE, 0)
    inadequate = review_counts.get(Evaluation.INADEQUATE, 0)

    # Did panels actually get independent model families?
    panels = session.execute(
        select(Review.manuscript_id, Review.round, Review.model_family).where(
            Review.kind == ReviewKind.PRE, Review.evaluation == Evaluation.ADEQUATE
        )
    ).all()
    grouped: dict[tuple, list[str]] = {}
    for m, r, f in panels:
        grouped.setdefault((m, r), []).append(f)
    diverse = sum(1 for fams in grouped.values() if len(set(fams)) == len(fams))

    verdicts = Counter(
        session.execute(
            select(Review.soundness_recommendation).where(Review.kind == ReviewKind.PRE, Review.evaluation == Evaluation.ADEQUATE)
        ).scalars()
    )
    return {
        "constitution": constitution.current().as_dict(),
        "operators": session.execute(select(func.count(Operator.id))).scalar_one(),
        "agents": session.execute(select(func.count(Agent.id))).scalar_one(),
        "manuscripts": {"total": sum(by_state.values()), "by_state": by_state},
        "reviews": {
            "adequate": adequate,
            "inadequate": inadequate,
            "awaiting_evaluation": review_counts.get(None, 0),
            "adequacy_rate": round(adequate / (adequate + inadequate), 3) if (adequate + inadequate) else None,
            "soundness_recommendations": dict(verdicts),
        },
        "panels": {"complete": len(grouped), "all_distinct_model_families": diverse},
        "credits": {
            "issued": credits.total_issued(session),
            "in_circulation": int(session.execute(select(func.coalesce(func.sum(CreditEntry.delta), 0))).scalar_one()),
        },
        "events": session.execute(select(func.count(Event.seq))).scalar_one(),
    }
