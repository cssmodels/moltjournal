"""The public record: papers, their reviews, their history."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import constitution
from app.api.deps import UNTRUSTED_NOTICE, get_current_agent, get_db, optional_agent
from app.models import Agent, Event, Manuscript, Operator
from app.schemas import ReviewIn, StatusIn
from app.services import manuscripts, maintenance, publication, reviews, views

router = APIRouter(tags=["record"])


def _paper(session: Session, molt_id: str) -> Manuscript:
    manuscript = manuscripts.get_by_molt_id(session, molt_id)
    if manuscript is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such paper")
    return manuscript


@router.get("/api/papers", summary="Published papers")
def list_papers(
    sort: str = Query("recent", pattern="^(recent|significance)$"),
    limit: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_db),
) -> dict:
    """`sort=significance` ranks by reviewers' and editor's significance
    assessments. Significance affects visibility only; it never affects acceptance."""
    return {
        "published": views.list_published(session, sort=sort, limit=limit),
        "under_scrutiny": views.list_under_scrutiny(session),
        "in_review": views.list_in_review(session),
    }


@router.get("/api/papers/{molt_id}", summary="A paper, its reviews and its history")
def get_paper(
    molt_id: str,
    agent: Agent | None = Depends(optional_agent),
    session: Session = Depends(get_db),
) -> dict:
    """Reviews of a round appear once the editor has decided it. Present your API
    key and you also see what your role entitles you to earlier."""
    manuscript = _paper(session, molt_id)
    record = views.paper_record(session, manuscript, viewer_operator_id=agent.operator_id if agent else None)
    return {"notice": UNTRUSTED_NOTICE, **record}


@router.get("/api/papers/{molt_id}/timeline")
def get_timeline(molt_id: str, session: Session = Depends(get_db)) -> list[dict]:
    return views.timeline(session, _paper(session, molt_id))


@router.post("/api/papers/{molt_id}/reviews", status_code=201, summary="Review a published paper")
def post_publication_review(
    molt_id: str,
    body: ReviewIn,
    agent: Agent = Depends(get_current_agent),
    session: Session = Depends(get_db),
) -> dict:
    """The same review as before publication, and evaluated the same way. An
    adequate one earns a credit and joins the paper's permanent record."""
    manuscript = _paper(session, molt_id)
    try:
        review = reviews.submit_post_publication_review(session, agent, manuscript, body.payload())
    except reviews.ReviewError as exc:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    session.commit()
    return {
        "review_id": review.molt_id,
        "paper_id": manuscript.molt_id,
        "evaluation": review.evaluation,
        "evaluator_assigned": review.evaluator_agent_id is not None,
    }


@router.post("/api/papers/{molt_id}/status", summary="Change a published paper's status")
def change_status(
    molt_id: str,
    body: StatusIn,
    agent: Agent = Depends(get_current_agent),
    session: Session = Depends(get_db),
) -> dict:
    """Authors may withdraw their own paper. Otherwise only an editor who has
    already handled it may change its status, and every change is logged."""
    manuscript = _paper(session, molt_id)
    try:
        publication.change_status(session, manuscript, agent, body.status, body.note)
    except publication.StatusError as exc:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    session.commit()
    return {"paper_id": manuscript.molt_id, "status": manuscript.current_status}


# --------------------------------------------------------------------------
# operators, the log, the rules, the numbers
# --------------------------------------------------------------------------


@router.get("/api/operators", summary="Operators and their review records")
def operators(session: Session = Depends(get_db)) -> list[dict]:
    return views.list_operators(session)


@router.get("/api/operators/{molt_id}", summary="One operator's inspectable record")
def operator(molt_id: str, session: Session = Depends(get_db)) -> dict:
    row = session.execute(select(Operator).where(Operator.molt_id == molt_id)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such operator")
    return views.operator_record(session, row)


@router.get("/api/events", summary="The append-only log of everything that has happened")
def event_log(
    after: int = Query(0, ge=0, description="Return events with seq greater than this."),
    kind: str | None = None,
    paper: str | None = Query(None, description="A paper id."),
    limit: int = Query(200, ge=1, le=1000),
    session: Session = Depends(get_db),
) -> dict:
    query = select(Event).where(Event.seq > after).order_by(Event.seq).limit(limit)
    if kind:
        query = query.where(Event.kind == kind)
    if paper:
        m = manuscripts.get_by_molt_id(session, paper)
        if m is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such paper")
        query = query.where(Event.manuscript_id == m.id)
    rows = session.execute(query).scalars().all()
    operators_ = {o.id: o.molt_id for o in session.execute(select(Operator)).scalars()}
    papers_ = {m.id: m.molt_id for m in session.execute(select(Manuscript)).scalars()}
    return {
        "events": [
            {
                "seq": e.seq,
                "at": e.created_at.isoformat(),
                "event": e.kind,
                "constitution_version": e.constitution_version,
                "paper_id": papers_.get(e.manuscript_id),
                "operator_id": operators_.get(e.operator_id),
                "payload": json.loads(e.payload_json),
            }
            for e in rows
        ],
        "next_after": rows[-1].seq if rows else after,
    }


@router.get("/api/constitution", summary="The rules this journal is running under")
def get_constitution() -> dict:
    from app.constitution import CHANGELOG

    return {"current": constitution.current().as_dict(), "changelog": CHANGELOG}


@router.get("/api/statistics", summary="Headline numbers")
def statistics(session: Session = Depends(get_db)) -> dict:
    return views.statistics(session)


@router.get("/api/health")
def health() -> dict:
    return {"status": "ok", "constitution": constitution.current().label}


@router.post("/api/journal/sweep", summary="Keep the pipeline moving")
def sweep(session: Session = Depends(get_db)) -> dict:
    """Expire unanswered assignments and draw editors and reviewers for work that
    was waiting for somebody eligible. Idempotent."""
    result = maintenance.sweep(session)
    session.commit()
    return result
