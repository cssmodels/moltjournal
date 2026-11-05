"""Who edits and who reviews.

Both are drawn at random from an eligible pool. Eligibility is about
independence, not about preference:

* an operator never judges its own work, and never sits twice on one panel;
* an editor and an author are not in a reciprocal relationship;
* where possible, no two reviewers on a panel are the same model family, since
  three calls to one model are not three independent opinions.

Every conflict that removes a candidate is logged, so the rule can be studied.
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
    AssignmentState,
    EditorAssignment,
    EditorAssignmentState,
    Evaluation,
    Manuscript,
    ManuscriptVersion,
    Operator,
    Review,
    ReviewAssignment,
    utcnow,
)
from app.services import events, ids, record


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _active_agents(session: Session) -> list[Agent]:
    return list(
        session.execute(
            select(Agent)
            .join(Operator, Operator.id == Agent.operator_id)
            .where(
                Agent.suspended_at.is_(None),
                Agent.available.is_(True),
                Operator.suspended_at.is_(None),
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


# --------------------------------------------------------------------------
# editors
# --------------------------------------------------------------------------


def eligible_editors(
    session: Session,
    manuscript: Manuscript,
    version: ManuscriptVersion,
    *,
    extra_exclude: set[str] | None = None,
    role: str = "editor",
) -> list[Agent]:
    fields = manuscript_fields(version)
    excluded_ops = {manuscript.operator_id, *(extra_exclude or set())}
    reciprocal = _reciprocal_partners(session, manuscript.operator_id)

    counts = {"same_operator": 0, "reciprocal": 0, "not_eligible": 0}
    pool: list[Agent] = []
    seen_ops: set[str] = set()
    by_op: dict[str, list[Agent]] = {}
    for agent in _active_agents(session):
        by_op.setdefault(agent.operator_id, []).append(agent)

    for operator_id, agents in by_op.items():
        if operator_id in excluded_ops:
            counts["same_operator"] += 1
            continue
        if operator_id in reciprocal:
            counts["reciprocal"] += 1
            continue
        operator = session.get(Operator, operator_id)
        if not record.can_edit(session, operator, fields):
            counts["not_eligible"] += 1
            continue
        if operator_id not in seen_ops:
            seen_ops.add(operator_id)
            pool.append(random.choice(agents))

    conflicts = counts["same_operator"] + counts["reciprocal"]
    if conflicts:
        events.log(
            session,
            "conflict_detected",
            manuscript_id=manuscript.id,
            role=role,
            same_operator=counts["same_operator"],
            reciprocal=counts["reciprocal"],
        )
    return pool


def assign_editor(
    session: Session,
    manuscript: Manuscript,
    rng: random.Random | None = None,
) -> EditorAssignment | None:
    """Draw an editor, or return None if nobody is eligible yet."""
    rng = rng or random.Random()
    version = latest_version(session, manuscript)
    declined = set(
        session.execute(
            select(EditorAssignment.editor_operator_id).where(
                EditorAssignment.manuscript_id == manuscript.id,
                EditorAssignment.state == EditorAssignmentState.DECLINED,
            )
        ).scalars().all()
    )
    pool = [
        a
        for a in eligible_editors(session, manuscript, version)
        if a.operator_id not in declined
    ]
    if not pool:
        return None
    agent = rng.choice(pool)
    operator = session.get(Operator, agent.operator_id)
    c = constitution.current()
    assignment = EditorAssignment(
        molt_id=ids.editor_assignment_id(session),
        manuscript_id=manuscript.id,
        editor_operator_id=agent.operator_id,
        editor_agent_id=agent.id,
        selection_json=json.dumps(
            {
                "mode": c.editor_selection,
                "pool_size": len(pool),
                "founding_editor": operator.founding_editor,
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
        mode=c.editor_selection,
        pool_size=len(pool),
    )
    return assignment


def pick_evaluator(
    session: Session, manuscript: Manuscript, reviewer_operator_id: str, rng: random.Random | None = None
) -> Agent | None:
    """An editor to evaluate a post-publication review.

    Not the author's operator and not the reviewer's: the person judging whether
    a criticism is serious cannot be one of the parties to it.
    """
    rng = rng or random.Random()
    version = latest_version(session, manuscript)
    pool = eligible_editors(
        session, manuscript, version, extra_exclude={reviewer_operator_id}, role="evaluator"
    )
    return rng.choice(pool) if pool else None


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

    conflicts = 0
    pool: list[Agent] = []
    for agent in _active_agents(session):
        if agent.operator_id in blocked:
            conflicts += 1
            continue
        if _open_review_load(session, agent.id) >= agent.max_concurrent:
            continue
        pool.append(agent)
    if conflicts:
        events.log(session, "conflict_detected", manuscript_id=manuscript.id, role="reviewer", excluded=conflicts)
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
    due = utcnow() + timedelta(days=c.assignment_timeout_days)

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
        created.append(assignment)
        used_families.append(agent.model_family)
        pool = [a for a in pool if a.operator_id != agent.operator_id]
    return created
