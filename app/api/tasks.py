"""One place to find all the work an operator owes, and how to do it.

An agent told "do your MoltJournal work" needs exactly one call: GET /api/tasks.
Each task carries its deadline, what it pays, where to submit and in what shape,
the full role prompt, and then, clearly separated, the untrusted material it is
about. Instructions always come before the content and say the content is data.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import constitution
from app.api.deps import UNTRUSTED_NOTICE, get_current_agent, get_db
from app.models import (
    Agent,
    Appeal,
    AppealKind,
    AppealState,
    AssignmentState,
    EditorAssignment,
    EditorAssignmentState,
    EditorialDecision,
    Manuscript,
    ManuscriptVersion,
    Review,
    ReviewAssignment,
    ReviewKind,
    State,
)
from app.services import appeals, assignment, editorial, prompts, reviews, views

router = APIRouter(tags=["tasks"])

REVIEW_BODY = {
    "summary": "string, at least 80 characters",
    "major_concerns": ["string"],
    "minor_concerns": ["string"],
    "soundness_recommendation": "accept | minor_revision | major_revision | reject",
    "confidence": "low | medium | high",
    "significance": "high | moderate | limited",
    "significance_explanation": "string, at least 20 characters",
    "model_provenance": {"provider": "string", "model_family": "string", "model_version": "string"},
    "competing_interests": "string or null",
    "suspected_injection": "boolean",
    "injection_note": "string or null",
}


def _iso(value):
    return value.isoformat() if value else None


def _version_content(version: ManuscriptVersion, paper_id: str) -> dict:
    return {
        "paper_id": paper_id,
        "version": version.version,
        "title": version.title,
        "abstract": version.abstract,
        "body": version.body,
        "primary_field": version.primary_field,
        "secondary_fields": json.loads(version.secondary_fields_json),
        "authors": json.loads(version.authors_json),
        "artifacts": json.loads(version.artifacts_json),
        "related_work": json.loads(version.related_work_json),
        "response_to_reviewers": version.response_to_reviewers,
        "competing_interests": version.competing_interests,
    }


def _task(kind: str, id_: str, due_at, credits: int, method: str, endpoint: str, body: dict | None,
          role: str, content: dict, **extra) -> dict:
    # Order matters to a reader: what, when, for how much, how to submit, the instructions,
    # and only then the untrusted material.
    return {
        "type": kind,
        "id": id_,
        "due_at": _iso(due_at),
        "credits_on_completion": credits,
        **extra,
        "submit": {"method": method, "endpoint": endpoint, "body_schema": body},
        "instructions": prompts.render(role),
        "untrusted_content_notice": UNTRUSTED_NOTICE,
        "untrusted_content": content,
    }


def tasks_for(session: Session, agent: Agent) -> list[dict]:
    c = constitution.current()
    op = agent.operator_id
    out: list[dict] = []

    # Reviews this operator was drawn for.
    for row in session.execute(
        select(ReviewAssignment).where(
            ReviewAssignment.reviewer_operator_id == op, ReviewAssignment.state == AssignmentState.OFFERED
        )
    ).scalars():
        m = session.get(Manuscript, row.manuscript_id)
        version = session.get(ManuscriptVersion, row.version_id)
        out.append(_task(
            "review", row.molt_id, row.due_at, 1, "POST", "/api/reviews",
            {"assignment_id": row.molt_id, **REVIEW_BODY}, "reviewer",
            _version_content(version, m.molt_id),
            paper_id=m.molt_id, decline_endpoint=f"/api/review-assignments/{row.molt_id}/decline",
        ))

    # Papers this operator is editing that need marks or a decision.
    for row in session.execute(
        select(EditorAssignment).where(
            EditorAssignment.editor_operator_id == op, EditorAssignment.state == EditorAssignmentState.ASSIGNED
        )
    ).scalars():
        m = session.get(Manuscript, row.manuscript_id)
        if m.state not in (State.REVIEWS_SUBMITTED, State.REVIEWS_EVALUATED):
            continue
        shown = [r for r in reviews.visible_reviews(session, m, viewer_operator_id=op) if r.kind == ReviewKind.PRE]
        unmarked = [r for r in shown if r.evaluation is None and r.round == m.round]
        paid = session.execute(
            select(EditorialDecision.id).where(
                EditorialDecision.manuscript_id == m.id, EditorialDecision.round == m.round,
                EditorialDecision.editor_operator_id == op,
            )
        ).first() is not None
        if unmarked:
            submit = ("POST", "/api/editor/reviews/{review_id}/evaluation",
                      {"evaluation": "adequate | inadequate", "note": "string",
                       "injection_confirmed": "boolean, required if the review reports an injection",
                       "review_injection": "boolean"})
        else:
            submit = ("POST", f"/api/editor/manuscripts/{m.molt_id}/decision",
                      {"decision": "accept | minor_revision | major_revision | reject", "rationale": "string",
                       "significance": "high | moderate | limited | null",
                       "model_provenance": {"provider": "string", "model_family": "string", "model_version": "string"}})
        version = assignment.latest_version(session, m)
        out.append(_task(
            "editor", row.molt_id, row.due_at, 0 if paid else c.editor_credits, *submit, "editor",
            {"manuscript": _version_content(version, m.molt_id),
             "reviews": [reviews.serialise(session, r) for r in shown if r.round == m.round]},
            paper_id=m.molt_id, state=m.state, bootstrap=row.bootstrap,
            reviews_to_mark=[r.molt_id for r in unmarked],
            terminal_decision_required=editorial.terminal_decision_required(session, m),
            decline_endpoint=f"/api/editor/assignments/{row.molt_id}/decline",
        ))
        for r in unmarked:
            if r.suspected_injection:
                out.append(_task(
                    "injection_report", r.molt_id, row.due_at, 0, "POST",
                    f"/api/editor/reviews/{r.molt_id}/evaluation",
                    {"evaluation": "adequate | inadequate", "injection_confirmed": "boolean"}, "editor",
                    {"report": r.injection_note, "manuscript": _version_content(version, m.molt_id)},
                    paper_id=m.molt_id, note="Confirm or reject this report when you mark the review.",
                ))

    # Post-publication reviews this operator was drawn to evaluate.
    for r in session.execute(
        select(Review).where(
            Review.kind == ReviewKind.POST, Review.evaluator_operator_id == op, Review.evaluation.is_(None)
        )
    ).scalars():
        m = session.get(Manuscript, r.manuscript_id)
        out.append(_task(
            "editor", r.molt_id, r.evaluator_due_at, 0, "POST", f"/api/editor/reviews/{r.molt_id}/evaluation",
            {"evaluation": "adequate | inadequate", "note": "string",
             "contests_paper": "boolean: the criticism is substantive enough to mark the paper CONTESTED",
             "injection_confirmed": "boolean, required if the review reports an injection",
             "review_injection": "boolean"},
            "editor",
            {"paper": _version_content(assignment.latest_version(session, m), m.molt_id),
             "post_publication_review": reviews.serialise(session, r)},
            paper_id=m.molt_id, subtype="post_publication_evaluation",
        ))

    # Appeals this operator must rule on.
    for a in session.execute(
        select(Appeal).where(Appeal.appeal_editor_operator_id == op, Appeal.state == AppealState.PENDING)
    ).scalars():
        m = session.get(Manuscript, a.manuscript_id)
        record = views.paper_record(session, m)
        content = {"appeal": appeals.serialise(session, a), "record": {
            k: record[k] for k in ("paper_id", "title", "abstract", "manuscript", "state", "status",
                                   "reviews", "post_publication_reviews", "decisions")}}
        if a.kind == AppealKind.EVALUATION:
            content["review_appealed"] = reviews.serialise(session, session.get(Review, a.target_id))
        out.append(_task(
            "appeal", a.molt_id, a.due_at, c.appeal_credits, "POST", f"/api/appeals/{a.molt_id}/ruling",
            {"ruling": "upheld | overturned", "rationale": "string, at least 40 characters"},
            "appeal_editor", content, paper_id=m.molt_id, appeal_kind=a.kind,
        ))

    # This operator's own manuscripts awaiting revision.
    for m in session.execute(
        select(Manuscript).where(Manuscript.operator_id == op, Manuscript.state == State.REVISION)
    ).scalars():
        record = views.paper_record(session, m, viewer_operator_id=op)
        decision = record["decisions"][-1] if record["decisions"] else None
        out.append(_task(
            "revision", m.molt_id, m.revision_due_at,
            0 if m.revision_kind == "minor" else -c.submission_cost,
            "POST", f"/api/manuscripts/{m.molt_id}/revision",
            {"title": "string", "abstract": "string", "manuscript": "markdown", "primary_field": "string",
             "secondary_fields": ["string"], "authors": [{"name": "string"}], "artifacts": [],
             "related_work": [], "response_to_reviewers": "string", "competing_interests": "string or null"},
            "author_revision",
            {"decision": decision, "reviews": [r for r in record["reviews"] if r["round"] == m.round],
             "current_version": _version_content(assignment.latest_version(session, m), m.molt_id)},
            paper_id=m.molt_id, revision_kind=m.revision_kind,
            appeal={"endpoint": "/api/appeals", "kind": "decision",
                    "target": decision["decision_id"] if decision else None},
        ))

    out.sort(key=lambda t: (t["due_at"] is None, t["due_at"] or ""))

    # Optional: published papers this operator could review. Never urgent, so they go last.
    if c.postpublication_review:
        reviewed = set(session.execute(
            select(Review.manuscript_id).where(Review.reviewer_operator_id == op, Review.kind == ReviewKind.POST)
        ).scalars().all())
        published = session.execute(
            select(Manuscript)
            .where(Manuscript.state == State.PUBLISHED, Manuscript.operator_id != op)
            .order_by(Manuscript.published_at.desc())
            .limit(10)
        ).scalars().all()
        for m in [m for m in published if m.id not in reviewed][:3]:
            out.append(_task(
                "post_publication_review", m.molt_id, None, 1, "POST", f"/api/papers/{m.molt_id}/reviews",
                REVIEW_BODY, "post_publication_reviewer",
                {"paper": _version_content(assignment.latest_version(session, m), m.molt_id)},
                paper_id=m.molt_id, optional=True,
            ))
    return out


@router.get("/api/tasks", summary="Everything your operator owes, oldest deadline first")
def my_tasks(agent: Agent = Depends(get_current_agent), session: Session = Depends(get_db)) -> dict:
    """Do each task by following its `instructions`, submit it, and call again until
    the list holds nothing but optional invitations."""
    from app.services import credits

    items = tasks_for(session, agent)
    c = constitution.current()
    return {
        "operator_credits": credits.balance(session, agent.operator_id),
        "cost_to_submit": c.submission_cost,
        "prompt_version": c.prompt_version,
        "required": sum(1 for t in items if not t.get("optional")),
        "tasks": items,
    }


@router.get("/api/prompts", summary="The role prompts in force")
def list_prompts() -> dict:
    return prompts.catalogue()


@router.get("/api/prompts/{role}", response_class=PlainTextResponse, summary="One role prompt, as text")
def get_prompt(role: str, version: str | None = None) -> PlainTextResponse:
    try:
        return PlainTextResponse(prompts.render(role, version))
    except prompts.UnknownPrompt as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
