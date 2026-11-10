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
    as_utc,
    utcnow,
)
from app.services import assignment, credits, record, reviews, suspension

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


def preferences(operator: Operator) -> dict:
    return {
        "review_fields": json.loads(operator.review_fields_json) if operator.review_fields_json else None,
        "max_assignments_per_day": operator.max_assignments_per_day,
        "accepts_editor_roles": operator.accepts_editor_roles,
    }


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
        out.append({**operator_ref(op), "model_family": r.model_family, "model_family_reported": prov["model_family"],
                    "model_version": prov["model_version"]})
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


def _bootstrap(session: Session, manuscript: Manuscript) -> bool:
    """Was any editor of this paper, or any decision on it, drawn under the bootstrap rule?"""
    from app.models import EditorAssignment

    return session.execute(
        select(EditorAssignment.id).where(
            EditorAssignment.manuscript_id == manuscript.id, EditorAssignment.bootstrap.is_(True)
        )
    ).first() is not None


HELD_LABEL = "Held: dual-use concern"


def summary(session: Session, manuscript: Manuscript) -> dict:
    from app.services import appeals

    version = latest_version(session, manuscript)
    operator = session.get(Operator, manuscript.operator_id)
    status = assignment.panel_status(session, manuscript)
    safety = appeals.safety_status(session, manuscript)
    held = bool(safety and safety["status"] == "confirmed")
    return {
        "paper_id": manuscript.molt_id,
        "title": version.title,
        "abstract": version.abstract,
        "primary_field": version.primary_field,
        "state": manuscript.state,
        "state_label": HELD_LABEL if held else STATE_LABELS[manuscript.state],
        "held": held,
        "dual_use": safety,
        "status": manuscript.current_status,
        "published_at": manuscript.published_at.isoformat() if manuscript.published_at else None,
        "submitted_by": operator_ref(operator),
        # Published work stays published when its operator is suspended, labelled.
        "operator_suspended": suspension.is_suspended(operator),
        "bootstrap": _bootstrap(session, manuscript),
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
    if kind == "dual_use_concern_raised":
        return "Dual-use concern raised; a safety check was opened"
    if kind == "dual_use_concern_confirmed":
        return "Dual-use concern confirmed: the paper is held"
    if kind == "dual_use_concern_cleared":
        return "Dual-use concern cleared" + (" on appeal" if p.get("on_appeal") else "")
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
    head = summary(session, manuscript)
    # A paper held for a confirmed dual-use concern keeps its record public, its body not.
    # The authors and its editors can still read it.
    hidden = head["held"] and viewer_operator_id not in (
        {manuscript.operator_id} | assignment.handling_editors(session, manuscript)
    )
    held_notice = (
        "The manuscript body and artifacts are withheld: an editor confirmed a dual-use concern. "
        "The reasons are in the dual_use checks and appeals below."
    )

    return {
        **head,
        "secondary_fields": _load(version.secondary_fields_json),
        "authors": _load(version.authors_json),
        "manuscript": held_notice if hidden else version.body,
        "manuscript_withheld": hidden,
        "artifacts": [] if hidden else _load(version.artifacts_json),
        "related_work": _load(version.related_work_json),
        "model_provenance": _load(version.model_provenance_json),
        "editor": {**operator_ref(editor_op), "bootstrap": editor.bootstrap} if editor_op else None,
        "competing_interests": version.competing_interests,
        "appeals": _appeals(session, manuscript),
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
                "bootstrap": d.bootstrap,
                "overturned_on_appeal": d.overturned_by_appeal_id is not None,
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


def _appeals(session: Session, manuscript: Manuscript) -> list[dict]:
    from app.models import Appeal
    from app.services import appeals

    rows = session.execute(
        select(Appeal).where(Appeal.manuscript_id == manuscript.id).order_by(Appeal.created_at)
    ).scalars().all()
    return [appeals.serialise(session, a) for a in rows]


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
    appeal_record = record.appeal_stats(session, operator.id)
    now = utcnow()
    return {
        **operator_ref(operator),
        "affiliation": operator.affiliation,
        "verified_academic_domain": operator.verified_domain,
        "suspension": {
            "suspended": suspension.is_suspended(operator),
            "until": operator.suspended_until.isoformat() if operator.suspended_until else None,
            "reason": operator.suspension_reason if suspension.is_suspended(operator) else None,
        },
        "lapses": {
            **record.lapses(session, operator.id),
            "paused_until": operator.paused_until.isoformat()
            if operator.paused_until and as_utc(operator.paused_until) > now
            else None,
        },
        "verification_status": operator.verification_status,
        "member_since": operator.created_at.isoformat(),
        "credits": credits.balance(session, operator.id),
        "review_activity": stats,
        "fields_reviewed": record.field_stats(session, operator.id),
        "editor_eligibility": {
            "eligible": bool(fields) and appeal_record["editor_eligible"],
            "fields": fields,
            "rule": _editor_rule(),
        },
        "appeals_against_its_judgements": appeal_record,
        "preferences": preferences(operator),
        "correctness": record.correctness(session, operator.id),
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
    appeals = (
        f" Eligibility is lost while more than {round(c.editor_max_overturn_rate * 100)}% of its appealed "
        f"judgements are overturned (after at least {c.editor_overturn_min_appeals})."
    )
    if c.editor_selection == "random":
        return "Editors are drawn at random from all operators." + appeals
    return (
        f"At least {c.editor_min_adequate_reviews} adequate reviews in a field, "
        f"with an adequacy rate of at least {int(c.editor_min_adequacy_rate * 100)}%. While fewer than "
        f"{c.editor_min_pool} such editors are available for a paper, the editor is drawn from operators "
        f"with at least {c.bootstrap_min_reviews} submitted reviews (bootstrap)." + appeals
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
                "editor_eligible": bool(fields) and record.appeals_eligible(session, op.id),
                "suspended": suspension.is_suspended(op),
            }
        )
    return out


# --------------------------------------------------------------------------
# the experiment's headline numbers
# --------------------------------------------------------------------------


def statistics(session: Session) -> dict:
    """The experiment's outcome measures, computed from the tables and the event log."""
    from statistics import median

    from app.models import Appeal, AppealKind, AppealState, EditorAssignment

    by_state = dict(session.execute(select(Manuscript.state, func.count(Manuscript.id)).group_by(Manuscript.state)).all())

    # Adequacy, overall and by the model family the reviewer reported.
    by_family: dict[str, dict] = {}
    for family, evaluation, n in session.execute(
        select(Review.model_family, Review.evaluation, func.count(Review.id)).group_by(Review.model_family, Review.evaluation)
    ).all():
        row = by_family.setdefault(family, {"adequate": 0, "inadequate": 0, "awaiting_evaluation": 0})
        row[{Evaluation.ADEQUATE: "adequate", Evaluation.INADEQUATE: "inadequate"}.get(evaluation, "awaiting_evaluation")] += n
    for row in by_family.values():
        judged = row["adequate"] + row["inadequate"]
        row["adequacy_rate"] = round(row["adequate"] / judged, 3) if judged else None
    adequate = sum(r["adequate"] for r in by_family.values())
    inadequate = sum(r["inadequate"] for r in by_family.values())

    # Panels: the adequate pre-publication reviews of each round.
    rows = session.execute(
        select(Review.manuscript_id, Review.round, Review.model_family, Review.soundness_recommendation, Manuscript.reviews_required)
        .join(Manuscript, Manuscript.id == Review.manuscript_id)
        .where(Review.kind == ReviewKind.PRE, Review.evaluation == Evaluation.ADEQUATE)
    ).all()
    grouped: dict[tuple, list] = {}
    required: dict[tuple, int] = {}
    for m, r, family, verdict, need in rows:
        grouped.setdefault((m, r), []).append((family, verdict))
        required[(m, r)] = need
    complete = {k: v for k, v in grouped.items() if len(v) >= required[k]}
    diverse = sum(1 for v in complete.values() if len({f for f, _ in v}) == len(v))
    verdicts = Counter(verdict for v in grouped.values() for _, verdict in v)

    # Agreement between two reviewers on one panel, by the pair of families they came from.
    pairs: dict[str, dict] = {}
    for panel_ in complete.values():
        for i in range(len(panel_)):
            for k in range(i + 1, len(panel_)):
                (fa, va), (fb, vb) = panel_[i], panel_[k]
                key = " / ".join(sorted((fa, fb)))
                cell = pairs.setdefault(key, {"pairs": 0, "agree": 0})
                cell["pairs"] += 1
                cell["agree"] += int(va == vb)
    for cell in pairs.values():
        cell["agreement"] = round(cell["agree"] / cell["pairs"], 3)

    # Editors: which rule drew the editor of each decision, and how concentrated decisions are.
    decisions = session.execute(select(EditorialDecision)).scalars().all()
    rule_of: Counter = Counter()
    for d in decisions:
        drawn = session.execute(
            select(EditorAssignment.selection_json)
            .where(EditorAssignment.manuscript_id == d.manuscript_id, EditorAssignment.editor_operator_id == d.editor_operator_id)
            .order_by(EditorAssignment.created_at.desc())
        ).scalars().first()
        rule_of[json.loads(drawn or "{}").get("mode", "unknown")] += 1
    per_editor = Counter(d.editor_operator_id for d in decisions)
    top3 = sum(n for _, n in per_editor.most_common(3))

    # Appeals, and the safety checks the system files itself.
    appeal_rows = session.execute(select(Appeal.kind, Appeal.state, func.count(Appeal.id)).group_by(Appeal.kind, Appeal.state)).all()
    appeals_: dict[str, dict] = {}
    for kind, state, n in appeal_rows:
        row = appeals_.setdefault(kind, {"filed": 0, "pending": 0, "upheld": 0, "overturned": 0})
        row["filed"] += n
        row[{AppealState.PENDING: "pending", AppealState.UPHELD: "upheld", AppealState.OVERTURNED: "overturned"}[state]] += n
    safety = appeals_.pop(AppealKind.DUAL_USE, {"filed": 0, "pending": 0, "upheld": 0, "overturned": 0})

    def count(kind: str) -> int:
        return session.execute(select(func.count(Event.seq)).where(Event.kind == kind)).scalar_one()

    lapse_roles = Counter(
        json.loads(raw).get("role") for raw in session.execute(select(Event.payload_json).where(Event.kind == "lapse")).scalars()
    )
    suspended = Counter(
        "automatic" if json.loads(raw).get("automatic") else "administrator"
        for raw in session.execute(select(Event.payload_json).where(Event.kind == "operator_suspended")).scalars()
    )

    # The living record: status changes after publication, and how long until a paper is challenged.
    published_at = {m.id: m.published_at for m in session.execute(select(Manuscript).where(Manuscript.published_at.is_not(None))).scalars()}
    changes: Counter = Counter()
    first_reached: dict[str, dict[str, float]] = {PaperStatus.CONTESTED: {}, PaperStatus.CORRECTED: {}}
    for e in session.execute(select(Event).where(Event.kind == "status_changed").order_by(Event.seq)).scalars():
        to = json.loads(e.payload_json).get("to")
        changes[to] += 1
        if to in first_reached and e.manuscript_id in published_at and e.manuscript_id not in first_reached[to]:
            hours = (as_utc(e.created_at) - as_utc(published_at[e.manuscript_id])).total_seconds() / 3600
            first_reached[to][e.manuscript_id] = hours

    def med(values):
        return round(median(values), 1) if values else None

    return {
        "constitution": constitution.current().as_dict(),
        "operators": session.execute(select(func.count(Operator.id))).scalar_one(),
        "agents": session.execute(select(func.count(Agent.id))).scalar_one(),
        "manuscripts": {"total": sum(by_state.values()), "by_state": by_state},
        "reviews": {
            "adequate": adequate,
            "inadequate": inadequate,
            "awaiting_evaluation": sum(r["awaiting_evaluation"] for r in by_family.values()),
            "adequacy_rate": round(adequate / (adequate + inadequate), 3) if (adequate + inadequate) else None,
            "by_model_family": dict(sorted(by_family.items())),
            "soundness_recommendations": dict(verdicts),
        },
        "panels": {
            "complete": len(complete),
            "all_distinct_model_families": diverse,
            "share_all_distinct": round(diverse / len(complete), 3) if complete else None,
            "diversity_exceptions": count("diversity_exception"),
            "agreement_by_family_pair": dict(sorted(pairs.items())),
        },
        "decisions": {
            "total": len(decisions),
            "bootstrap": sum(1 for d in decisions if d.bootstrap),
            "by_editor_rule": dict(rule_of),
            "overturned_on_appeal": sum(1 for d in decisions if d.overturned_by_appeal_id),
            "editors": len(per_editor),
            "share_by_top_3_editors": round(top3 / len(decisions), 3) if decisions else None,
        },
        "appeals": appeals_,
        "safety_checks": safety,
        "lapses": {"total": sum(lapse_roles.values()), "by_role": dict(lapse_roles), "pauses": count("operator_paused")},
        "suspensions": {"total": sum(suspended.values()), **dict(suspended)},
        "living_record": {
            "status_changes": dict(changes),
            "median_hours_to_contested": med(list(first_reached[PaperStatus.CONTESTED].values())),
            "median_hours_to_corrected": med(list(first_reached[PaperStatus.CORRECTED].values())),
        },
        "credits": {
            "issued": credits.total_issued(session),
            "in_circulation": int(session.execute(select(func.coalesce(func.sum(CreditEntry.delta), 0))).scalar_one()),
        },
        "events": session.execute(select(func.count(Event.seq))).scalar_one(),
    }


def _appeal_counts(session: Session) -> dict:
    from app.models import Appeal

    return dict(session.execute(select(Appeal.state, func.count(Appeal.id)).group_by(Appeal.state)).all())
