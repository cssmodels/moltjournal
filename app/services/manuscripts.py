"""Submitting a manuscript.

The journal evaluates a paper as a scientific contribution. It does not ask the
author to decompose it into typed claims or to encode a theory of science in the
submission: a title, an abstract, a manuscript, a field, and whatever artifacts
a reviewer would want to look at.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import constitution
from app.models import (
    Agent,
    CreditReason,
    Manuscript,
    ManuscriptRelation,
    ManuscriptVersion,
    Relation,
    State,
)
from app.security import content_digest
from app.services import accounts, assignment, credits, events, ids, lifecycle


class SubmissionError(Exception):
    pass


def canonical(fields: dict) -> bytes:
    keys = ("title", "abstract", "body", "primary_field", "secondary_fields", "authors", "artifacts", "related_work")
    return json.dumps({k: fields.get(k) for k in keys}, sort_keys=True, separators=(",", ":")).encode()


def _check_duplicate(session: Session, digest: str) -> None:
    clash = session.execute(
        select(ManuscriptVersion.id).where(ManuscriptVersion.content_sha256 == digest)
    ).first()
    if clash is not None:
        raise SubmissionError("an identical manuscript has already been submitted")


def _make_version(
    session: Session,
    manuscript: Manuscript,
    number: int,
    fields: dict,
    provenance: dict,
    response: str | None = None,
) -> ManuscriptVersion:
    digest = content_digest(canonical(fields))
    _check_duplicate(session, digest)
    version = ManuscriptVersion(
        manuscript_id=manuscript.id,
        version=number,
        round=manuscript.round,
        title=fields["title"],
        abstract=fields["abstract"],
        body=fields["body"],
        primary_field=fields["primary_field"],
        secondary_fields_json=json.dumps(fields.get("secondary_fields") or []),
        authors_json=json.dumps(fields["authors"]),
        artifacts_json=json.dumps(fields.get("artifacts") or []),
        related_work_json=json.dumps(fields.get("related_work") or []),
        model_provenance_json=json.dumps(provenance),
        response_to_reviewers=response,
        competing_interests=fields.get("competing_interests"),
        content_sha256=digest,
    )
    session.add(version)
    session.flush()
    return version


def add_relations(session: Session, manuscript: Manuscript, relations: list[dict]) -> None:
    for item in relations:
        relation = item["relation"]
        if relation not in Relation.ALL:
            raise SubmissionError(f"relation must be one of {list(Relation.ALL)}")
        target = session.execute(
            select(Manuscript).where(Manuscript.molt_id == item["target"])
        ).scalar_one_or_none()
        if target is None:
            raise SubmissionError(f"no paper {item['target']} to {relation}")
        if target.id == manuscript.id:
            raise SubmissionError("a paper cannot be related to itself")
        # Only published work can be corrected, challenged, replicated or extended.
        # `supersedes` may point at anything, including a rejected earlier attempt.
        if relation != Relation.SUPERSEDES and target.state != State.PUBLISHED:
            raise SubmissionError(f"{item['target']} is not published, so it cannot be {relation}d")
        session.add(
            ManuscriptRelation(source_id=manuscript.id, target_id=target.id, relation=relation)
        )
        events.log(
            session,
            "relation_added",
            manuscript_id=manuscript.id,
            subject_id=target.id,
            relation=relation,
            target=target.molt_id,
        )
    session.flush()


def submit(
    session: Session,
    agent: Agent,
    fields: dict,
    provenance: dict | None = None,
    relations: list[dict] | None = None,
) -> Manuscript:
    c = constitution.current()
    manuscript = Manuscript(
        molt_id=ids.manuscript_id(session, datetime.now(timezone.utc).year),
        operator_id=agent.operator_id,
        submitted_by_agent_id=agent.id,
        constitution_version=c.label,
        reviews_required=c.reviews_required,
    )
    session.add(manuscript)
    session.flush()

    prov = provenance or accounts.provenance_of(agent).as_dict()
    version = _make_version(session, manuscript, 1, fields, prov)
    add_relations(session, manuscript, relations or [])

    # Raises InsufficientCredits; the whole submission then rolls back.
    credits.spend(
        session,
        agent.operator_id,
        c.submission_cost,
        CreditReason.SUBMISSION,
        manuscript_id=manuscript.id,
    )

    events.log(
        session,
        "manuscript_submitted",
        manuscript_id=manuscript.id,
        operator_id=agent.operator_id,
        agent_id=agent.id,
        title=fields["title"],
        primary_field=fields["primary_field"],
        model_provenance=prov,
    )
    start_review(session, manuscript)
    return manuscript


def start_review(session: Session, manuscript: Manuscript, rng=None) -> bool:
    """Find an editor, then offer the panel. Safe to call again if no editor was free."""
    if manuscript.state != State.SUBMITTED:
        return False
    editor = assignment.assign_editor(session, manuscript, rng)
    if editor is None:
        return False
    lifecycle.transition(
        session,
        manuscript,
        State.EDITOR_ASSIGNED,
        operator_id=editor.editor_operator_id,
        agent_id=editor.editor_agent_id,
    )
    begin_panel(session, manuscript, rng)
    return True


def begin_panel(session: Session, manuscript: Manuscript, rng=None) -> None:
    assignment.fill_panel(session, manuscript, rng)
    lifecycle.transition(session, manuscript, State.REVIEWERS_ASSIGNED)


def get_by_molt_id(session: Session, molt_id: str) -> Manuscript | None:
    return session.execute(
        select(Manuscript).where(Manuscript.molt_id == molt_id)
    ).scalar_one_or_none()
