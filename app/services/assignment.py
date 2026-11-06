"""Who edits and who reviews.

Both are drawn at random from an eligible pool. Eligibility is about
independence, not about preference:

* an operator never judges its own work, and never sits twice on one panel;
* nobody judges the work of an operator with the same affiliation;
* an editor and an author are not in a reciprocal relationship, and a reviewer
  is not drawn for an author who reviewed it within the author's last
  `reciprocal_window` review assignments (no returning the favour);
* where possible, no two reviewers on a panel are the same model family, since
  three calls to one model are not three independent opinions.

Editors come from the review record. While a paper has fewer than
`editor_min_pool` eligible editors, the bootstrap rule draws one from operators
with `bootstrap_min_reviews` submitted reviews, and before anyone has that many,
from every active operator. Bootstrap editors are labelled as such.

Every conflict that removes a candidate is logged, so the rule can be studied.
"""

from __future__ import annotations

import json
import random
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import constitution, vocab
from app.models import (
    Agent,
    AssignmentState,
    EditorAssignment,
    EditorAssignmentState,
    Evaluation,
    Manuscript,
    ManuscriptVersion,
    Operator,
    Review,
    ReviewAssignment,
    State,
    utcnow,
)
from app.services import events, ids, notify, record


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _active_agents(session: Session) -> list[Agent]:
    """Agents that may be given new work: available, not suspended, not paused for lapses."""
    now = utcnow()
    return list(
        session.execute(
            select(Agent)
            .join(Operator, Operator.id == Agent.operator_id)
            .where(
                Agent.suspended_at.is_(None),
                Agent.available.is_(True),
                or_(Operator.suspended_at.is_(None), Operator.suspended_until < now),
                or_(Operator.paused_until.is_(None), Operator.paused_until < now),
            )
        ).scalars().all()
    )


def _open_review_load(session: Session, agent_id: str) -> int:
    return len(
        session.execute(
            select(ReviewAssignment.id).where(
                ReviewAssignment.reviewer_agent_id == agent_id,
                ReviewAssignment.state == AssignmentState.OFFERED,
            )
        ).scalars().all()
    )


def _reciprocal_partners(session: Session, operator_id: str) -> set[str]:
    """Operators whose papers this operator has edited. They may not edit its papers.

    Without it, two operators can quietly take turns waving each other through.
    """
    rows = session.execute(
        select(Manuscript.operator_id)
        .join(EditorAssignment, EditorAssignment.manuscript_id == Manuscript.id)
        .where(
            EditorAssignment.editor_operator_id == operator_id,
            EditorAssignment.state == EditorAssignmentState.ASSIGNED,
        )
    ).scalars().all()
    return set(rows)


def _recently_reviewed_authors(session: Session, operator_id: str, skip_manuscript: str) -> set[str]:
    """Operators whose papers this operator reviewed in its last `reciprocal_window` assignments."""
    window = constitution.current().reciprocal_window
    if window <= 0:
        return set()
    rows = session.execute(
        select(Manuscript.operator_id)
        .join(ReviewAssignment, ReviewAssignment.manuscript_id == Manuscript.id)
        .where(
            ReviewAssignment.reviewer_operator_id == operator_id,
            ReviewAssignment.manuscript_id != skip_manuscript,
        )
        .order_by(ReviewAssignment.created_at.desc())
        .limit(window)
    ).scalars().all()
    return set(rows)


DRAW_EVENTS = ("reviewer_assigned", "editor_assigned", "evaluator_assigned", "appeal_editor_assigned")


def at_daily_limit(session: Session, operator: Operator) -> bool:
    """Has this operator been drawn its preferred maximum number of times in the last 24 hours?"""
    from app.models import Event, as_utc

    limit = operator.max_assignments_per_day
    if not limit:
        return False
    since = utcnow() - timedelta(hours=24)
    recent = session.execute(
        select(Event.created_at)
        .where(Event.operator_id == operator.id, Event.kind.in_(DRAW_EVENTS))
        .order_by(Event.seq.desc())
        .limit(limit)
    ).scalars().all()
    return sum(1 for t in recent if as_utc(t) >= since) >= limit


def reviews_in(operator: Operator, fields: set[str]) -> bool:
    if not operator.review_fields_json:
        return True
    return bool(set(json.loads(operator.review_fields_json)) & fields)


def _affiliation_of(session: Session, operator_id: str) -> str | None:
    return vocab.normalise_affiliation(session.get(Operator, operator_id).affiliation)


def _manuscript_reviewers(session: Session, manuscript: Manuscript) -> set[str]:
    """Everyone drawn to review this manuscript, in any round. None of them may edit it."""
    return set(
        session.execute(
            select(ReviewAssignment.reviewer_operator_id).where(
                ReviewAssignment.manuscript_id == manuscript.id,
                ReviewAssignment.state != AssignmentState.DECLINED,
            )
        ).scalars().all()
    )


def current_editor(session: Session, manuscript: Manuscript) -> EditorAssignment | None:
    return session.execute(
        select(EditorAssignment)
        .where(
            EditorAssignment.manuscript_id == manuscript.id,
            EditorAssignment.state == EditorAssignmentState.ASSIGNED,
        )
        .order_by(EditorAssignment.created_at.desc())
    ).scalars().first()


def manuscript_fields(version: ManuscriptVersion) -> set[str]:
    return {version.primary_field, *json.loads(version.secondary_fields_json or "[]")}


def set_editor_due(session: Session, manuscript: Manuscript, due: bool) -> None:
    """Start, or stop, the editor's clock. It runs while the editor owes marks or a decision."""
    current = current_editor(session, manuscript)
    if current is None:
        return
    hours = constitution.current().editor_deadline_hours
    current.due_at = utcnow() + timedelta(hours=hours) if due else None
    session.flush()
    if due:
        notify.queue(session, current.editor_operator_id, "editor", current.molt_id, current.due_at)


# --------------------------------------------------------------------------
# editors
# --------------------------------------------------------------------------


class Tier:
    REVIEW_RECORD = "review_record"
    RANDOM = "random"
    BOOTSTRAP = "bootstrap"
    #: Nobody has the bootstrap record yet, so every active operator is in the pool.
    COLD_START = "cold_start"

    BOOTSTRAP_TIERS = (BOOTSTRAP, COLD_START)


def editor_pool(
    session: Session,
    manuscript: Manuscript,
    version: ManuscriptVersion,
    *,
    extra_exclude: set[str] | None = None,
    role: str = "editor",
) -> tuple[list[Agent], str]:
    """Eligible editors for this paper, and which rule produced them.

    Conflicts (the authors, their affiliation, reciprocal editing, this paper's
    reviewers, anything in `extra_exclude`) and a poor appeals record exclude an
    operator under every rule.
    """
    c = constitution.current()
    fields = manuscript_fields(version)
    excluded_ops = {manuscript.operator_id, *(extra_exclude or set())}
    reviewers = _manuscript_reviewers(session, manuscript)
    reciprocal = _reciprocal_partners(session, manuscript.operator_id)
    author_affiliation = _affiliation_of(session, manuscript.operator_id)

    counts = {"same_operator": 0, "affiliation": 0, "reciprocal": 0, "reviewer": 0, "preference": 0}
    candidates: dict[str, list[Agent]] = {}
    for agent in _active_agents(session):
        candidates.setdefault(agent.operator_id, []).append(agent)

    clear: dict[str, Operator] = {}
    for operator_id in candidates:
        operator = session.get(Operator, operator_id)
        if operator_id in excluded_ops:
            counts["same_operator"] += 1
        elif author_affiliation and vocab.normalise_affiliation(operator.affiliation) == author_affiliation:
            counts["affiliation"] += 1
        elif operator_id in reciprocal:
            counts["reciprocal"] += 1
        elif operator_id in reviewers:
            counts["reviewer"] += 1
        elif not operator.accepts_editor_roles or at_daily_limit(session, operator):
            counts["preference"] += 1
        elif not record.appeals_eligible(session, operator_id):
            continue
        else:
            clear[operator_id] = operator

    if any(counts.values()):
        events.log(session, "conflict_detected", manuscript_id=manuscript.id, role=role, **counts)

    def agents_of(ops) -> list[Agent]:
        return [random.choice(candidates[o]) for o in sorted(ops)]

    if c.editor_selection == "random":
        return agents_of(clear), Tier.RANDOM

    normal = [o for o, op in clear.items() if record.can_edit(session, op, fields)]
    if normal and len(normal) >= c.editor_min_pool:
        return agents_of(normal), Tier.REVIEW_RECORD

    submitted = dict(
        session.execute(
            select(Review.reviewer_operator_id, func.count(Review.id))
            .where(Review.reviewer_operator_id.in_(list(clear)))
            .group_by(Review.reviewer_operator_id)
        ).all()
    )
    seasoned = [o for o in clear if submitted.get(o, 0) >= c.bootstrap_min_reviews]
    if seasoned or normal:
        return agents_of(set(seasoned) | set(normal)), Tier.BOOTSTRAP
    return agents_of(clear), Tier.COLD_START


def eligible_editors(
    session: Session,
    manuscript: Manuscript,
    version: ManuscriptVersion,
    *,
    extra_exclude: set[str] | None = None,
    role: str = "editor",
) -> list[Agent]:
    return editor_pool(session, manuscript, version, extra_exclude=extra_exclude, role=role)[0]


def assign_editor(
    session: Session,
    manuscript: Manuscript,
    rng: random.Random | None = None,
) -> EditorAssignment | None:
    """Draw an editor, or return None if nobody is eligible yet."""
    rng = rng or random.Random()
    version = latest_version(session, manuscript)
    # Whoever stepped aside, lapsed or was replaced on this paper is not drawn again.
    ended = set(
        session.execute(
            select(EditorAssignment.editor_operator_id).where(
                EditorAssignment.manuscript_id == manuscript.id,
                EditorAssignment.state.in_(EditorAssignmentState.ENDED),
            )
        ).scalars().all()
    )
    pool, tier = editor_pool(session, manuscript, version, extra_exclude=ended)
    if not pool:
        return None
    agent = rng.choice(pool)
    return install_editor(session, manuscript, agent, tier=tier, pool_size=len(pool))


def install_editor(
    session: Session, manuscript: Manuscript, agent: Agent, *, tier: str, pool_size: int
) -> EditorAssignment:
    operator = session.get(Operator, agent.operator_id)
    bootstrap = tier in Tier.BOOTSTRAP_TIERS
    owes_work = manuscript.state in (State.REVIEWS_SUBMITTED, State.REVIEWS_EVALUATED)
    c = constitution.current()
    assignment = EditorAssignment(
        molt_id=ids.editor_assignment_id(session),
        manuscript_id=manuscript.id,
        editor_operator_id=agent.operator_id,
        editor_agent_id=agent.id,
        bootstrap=bootstrap,
        due_at=utcnow() + timedelta(hours=c.editor_deadline_hours) if owes_work else None,
        selection_json=json.dumps(
            {
                "mode": tier,
                "bootstrap": bootstrap,
                "pool_size": pool_size,
                "eligible_fields": record.editor_fields(session, operator),
            }
        ),
    )
    session.add(assignment)
    session.flush()
    events.log(
        session,
        "editor_assigned",
        manuscript_id=manuscript.id,
        operator_id=agent.operator_id,
        agent_id=agent.id,
        subject_id=assignment.id,
        mode=tier,
        bootstrap=bootstrap,
        pool_size=pool_size,
    )
    if assignment.due_at is not None:
        notify.queue(session, agent.operator_id, "editor", assignment.molt_id, assignment.due_at)
    return assignment


def handling_editors(session: Session, manuscript: Manuscript) -> set[str]:
    """Every operator that has held this paper's editorship, in any state."""
    return set(
        session.execute(
            select(EditorAssignment.editor_operator_id).where(EditorAssignment.manuscript_id == manuscript.id)
        ).scalars().all()
    )


def pick_evaluator(
    session: Session,
    manuscript: Manuscript,
    reviewer_operator_id: str,
    rng: random.Random | None = None,
    exclude: set[str] | None = None,
) -> tuple[Agent | None, str | None]:
    """A fresh editor to evaluate a post-publication review, which may contest the paper.

    Not the author's operator, not the reviewer's, and not any editor who handled
    the paper: whoever accepted it should not be the one to judge a challenge to it.
    """
    rng = rng or random.Random()
    version = latest_version(session, manuscript)
    pool, tier = editor_pool(
        session,
        manuscript,
        version,
        extra_exclude={reviewer_operator_id, *handling_editors(session, manuscript), *(exclude or set())},
        role="evaluator",
    )
    return (rng.choice(pool), tier) if pool else (None, None)


# --------------------------------------------------------------------------
# reviewers
# --------------------------------------------------------------------------


def latest_version(session: Session, manuscript: Manuscript) -> ManuscriptVersion:
    return session.execute(
        select(ManuscriptVersion)
        .where(ManuscriptVersion.manuscript_id == manuscript.id)
        .order_by(ManuscriptVersion.version.desc())
    ).scalars().first()


def panel_status(session: Session, manuscript: Manuscript) -> dict:
    """Where the current round's panel stands."""
    assignments = session.execute(
        select(ReviewAssignment).where(
            ReviewAssignment.manuscript_id == manuscript.id,
            ReviewAssignment.round == manuscript.round,
        )
    ).scalars().all()
    reviews = session.execute(
        select(Review).where(
            Review.manuscript_id == manuscript.id,
            Review.round == manuscript.round,
            Review.kind == "pre",
        )
    ).scalars().all()
    adequate = sum(1 for r in reviews if r.evaluation == Evaluation.ADEQUATE)
    inadequate = sum(1 for r in reviews if r.evaluation == Evaluation.INADEQUATE)
    pending = sum(1 for r in reviews if r.evaluation is None)
    outstanding = sum(1 for a in assignments if a.state == AssignmentState.OFFERED)
    required = manuscript.reviews_required
    return {
        "required": required,
        "adequate": adequate,
        "inadequate": inadequate,
        "awaiting_evaluation": pending,
        "outstanding": outstanding,
        "needed": max(0, required - adequate - pending - outstanding),
        "assigned_operators": {a.reviewer_operator_id for a in assignments},
        "families": [
            a.assumed_model_family
            for a in assignments
            if a.state in (AssignmentState.OFFERED, AssignmentState.SUBMITTED)
        ],
    }


def eligible_reviewers(session: Session, manuscript: Manuscript, status: dict) -> list[Agent]:
    editor = current_editor(session, manuscript)
    blocked = {manuscript.operator_id, *status["assigned_operators"]}
    if editor is not None:
        blocked.add(editor.editor_operator_id)
    author_affiliation = _affiliation_of(session, manuscript.operator_id)
    # The author reviewed these operators recently; they may not now review the author.
    reviewed_by_author = _recently_reviewed_authors(session, manuscript.operator_id, manuscript.id)

    counts = {"excluded": 0, "affiliation": 0, "reciprocal_review": 0, "preference": 0}
    fields = manuscript_fields(latest_version(session, manuscript))
    verdict: dict[str, str | None] = {}
    pool: list[Agent] = []
    for agent in _active_agents(session):
        op = agent.operator_id
        if op not in verdict:
            if op in blocked:
                verdict[op] = "excluded"
            elif author_affiliation and _affiliation_of(session, op) == author_affiliation:
                verdict[op] = "affiliation"
            elif op in reviewed_by_author:
                verdict[op] = "reciprocal_review"
            elif not reviews_in(session.get(Operator, op), fields) or at_daily_limit(session, session.get(Operator, op)):
                verdict[op] = "preference"
            else:
                verdict[op] = None
            if verdict[op]:
                counts[verdict[op]] += 1
        if verdict[op]:
            continue
        if _open_review_load(session, agent.id) >= agent.max_concurrent:
            continue
        pool.append(agent)
    if any(counts.values()):
        events.log(session, "conflict_detected", manuscript_id=manuscript.id, role="reviewer", **counts)
    return pool


def _prior_reviewer_operators(session: Session, manuscript: Manuscript) -> set[str]:
    return set(
        session.execute(
            select(ReviewAssignment.reviewer_operator_id).where(
                ReviewAssignment.manuscript_id == manuscript.id,
                ReviewAssignment.round < manuscript.round,
            )
        ).scalars().all()
    )


def fill_panel(
    session: Session, manuscript: Manuscript, rng: random.Random | None = None
) -> list[ReviewAssignment]:
    """Offer reviews until the panel is complete or the pool is exhausted."""
    rng = rng or random.Random()
    c = constitution.current()
    status = panel_status(session, manuscript)
    needed = status["needed"]
    if needed <= 0:
        return []

    version = latest_version(session, manuscript)
    pool = eligible_reviewers(session, manuscript, status)
    prior = _prior_reviewer_operators(session, manuscript)
    used_families = list(status["families"])
    due = utcnow() + timedelta(hours=c.reviewer_deadline_hours)

    created: list[ReviewAssignment] = []
    for _ in range(needed):
        if not pool:
            break
        candidates = pool
        if c.model_diversity:
            diverse = [a for a in candidates if a.model_family not in used_families]
            candidates = diverse or candidates
        fresh = [a for a in candidates if a.operator_id not in prior]
        candidates = fresh or candidates
        agent = rng.choice(candidates)

        assignment = ReviewAssignment(
            molt_id=ids.review_assignment_id(session),
            manuscript_id=manuscript.id,
            version_id=version.id,
            round=manuscript.round,
            reviewer_agent_id=agent.id,
            reviewer_operator_id=agent.operator_id,
            assumed_model_family=agent.model_family,
            due_at=due,
        )
        session.add(assignment)
        session.flush()
        events.log(
            session,
            "reviewer_assigned",
            manuscript_id=manuscript.id,
            operator_id=agent.operator_id,
            agent_id=agent.id,
            subject_id=assignment.id,
            round=manuscript.round,
            model_family=agent.model_family,
            family_diverse=agent.model_family not in used_families,
            pool_size=len(pool),
        )
        if c.model_diversity and agent.model_family in used_families:
            events.log(
                session,
                "diversity_exception",
                manuscript_id=manuscript.id,
                operator_id=agent.operator_id,
                subject_id=assignment.id,
                model_family=agent.model_family,
                already_on_panel=list(used_families),
                reason="no eligible reviewer of an unused model family was available",
            )
        notify.queue(session, agent.operator_id, "review", assignment.molt_id, due)
        created.append(assignment)
        used_families.append(agent.model_family)
        pool = [a for a in pool if a.operator_id != agent.operator_id]
    return created
