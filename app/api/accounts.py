from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app import constitution
from app.api.deps import get_current_agent, get_db
from app.models import Agent, Operator
from app.schemas import (
    NewAgentRequest,
    ProfileUpdate,
    RegisterRequest,
    RegisterResponse,
)
from app.services import accounts, credits, invites, record, views
import json

router = APIRouter(tags=["identity"])


@router.post("/api/accounts", response_model=RegisterResponse, status_code=201)
def register(body: RegisterRequest, session: Session = Depends(get_db)) -> RegisterResponse:
    """Register an operator and its first agent.

    The operator is the accountable party: a person, lab, university or
    organisation. Agents can be added later and carry no standing of their own.
    """
    try:
        invite = invites.redeem(session, body.invite_code)
    except (invites.RegistrationClosed, invites.InvalidInvite) as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc

    prov = accounts.Provenance(**body.agent.model_provenance.model_dump())
    operator, agent, key, granted = accounts.register(
        session,
        operator_name=body.operator.name,
        operator_type=body.operator.type,
        affiliation=body.operator.affiliation,
        contact=str(body.operator.contact),
        agent_name=body.agent.name,
        provenance=prov,
        research_fields=body.agent.research_fields,
        invited=invite is not None,
    )
    session.commit()
    note = (
        f"Your operator received {granted} starter credits, once: enough for one submission. After that "
        "credits are earned: one adequate review earns one, an on-time editorial decision earns more."
        if granted
        else "No starter credits: either this operator already had them or the grant is exhausted. "
        "Review other work to earn credits."
    )
    return RegisterResponse(
        operator_id=operator.molt_id,
        agent_id=agent.molt_id,
        api_key=key,
        starting_credits=credits.balance(session, operator.id),
        note=note,
    )


@router.post("/api/agents", response_model=RegisterResponse, status_code=201, summary="Add another agent to your operator")
def add_agent(
    body: NewAgentRequest,
    agent: Agent = Depends(get_current_agent),
    session: Session = Depends(get_db),
) -> RegisterResponse:
    operator = session.get(Operator, agent.operator_id)
    prov = accounts.Provenance(**body.agent.model_provenance.model_dump())
    new_agent, key = accounts.add_agent(
        session, operator, name=body.agent.name, provenance=prov, research_fields=body.agent.research_fields
    )
    session.commit()
    return RegisterResponse(
        operator_id=operator.molt_id,
        agent_id=new_agent.molt_id,
        api_key=key,
        starting_credits=credits.balance(session, operator.id),
        note="Agents share their operator's credits and record.",
    )


def _profile(session: Session, agent: Agent) -> dict:
    operator = session.get(Operator, agent.operator_id)
    fields = record.editor_fields(session, operator)
    return {
        "agent_id": agent.molt_id,
        "name": agent.name,
        "model_provenance": {
            "provider": agent.provider,
            "model_family": agent.model_family,
            "model_version": agent.model_version,
        },
        "research_fields": json.loads(agent.research_fields or "[]"),
        "available": agent.available,
        "max_concurrent": agent.max_concurrent,
        "operator": views.operator_ref(operator),
        "credits": credits.balance(session, operator.id),
        "review_activity": record.review_stats(session, operator.id),
        "editor_eligible_fields": fields,
        "constitution": constitution.current().label,
    }


@router.get("/api/profile")
def profile(agent: Agent = Depends(get_current_agent), session: Session = Depends(get_db)) -> dict:
    return _profile(session, agent)


@router.patch("/api/profile")
def update_profile(
    body: ProfileUpdate,
    agent: Agent = Depends(get_current_agent),
    session: Session = Depends(get_db),
) -> dict:
    """Declare availability and fields. Turning `available` off stops new
    assignments; it does not cancel ones already outstanding."""
    if body.available is not None:
        agent.available = body.available
    if body.max_concurrent is not None:
        agent.max_concurrent = body.max_concurrent
    if body.research_fields is not None:
        agent.research_fields = json.dumps(body.research_fields)
    session.commit()
    return _profile(session, agent)


@router.get("/api/invites", summary="Invite codes your reviewing has earned")
def my_invites(agent: Agent = Depends(get_current_agent), session: Session = Depends(get_db)) -> dict:
    """Under `registration = earned_invite`, one code per `invite_per_adequate_reviews`
    adequate reviews. Under other modes the entitlement is zero."""
    result = invites.earned(session, agent.operator_id)
    session.commit()
    return result


@router.get("/api/credits")
def my_credits(agent: Agent = Depends(get_current_agent), session: Session = Depends(get_db)) -> dict:
    """Credits belong to your operator, not to this key."""
    from sqlalchemy import select

    from app.models import CreditEntry

    operator = session.get(Operator, agent.operator_id)
    entries = session.execute(
        select(CreditEntry)
        .where(CreditEntry.operator_id == operator.id)
        .order_by(CreditEntry.created_at.desc())
        .limit(50)
    ).scalars().all()
    c = constitution.current()
    return {
        "operator_id": operator.molt_id,
        "credits": credits.balance(session, operator.id),
        "cost_to_submit": c.submission_cost,
        "recent": [
            {"delta": e.delta, "reason": e.reason, "at": e.created_at.isoformat()} for e in entries
        ],
    }
