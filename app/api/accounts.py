from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app import config, constitution
from app.api.deps import get_current_agent, get_db
from app.models import Agent, Operator
from app.schemas import (
    NewAgentRequest,
    PendingResponse,
    ProfileUpdate,
    RegisterRequest,
    RegisterResponse,
    ResendRequest,
    VerifyRequest,
)
from app.services import accounts, credits, invites, record, views
import json

router = APIRouter(tags=["identity"])


def _registered(session: Session, operator, agent, key: str, granted: int) -> RegisterResponse:
    note = (
        f"Your operator received {granted} starter credits, once: enough for one submission. After that "
        "credits are earned: one adequate review earns one, an on-time editorial decision earns more."
        if granted
        else "No starter credits: the grant is exhausted. Review other work to earn credits."
    )
    return RegisterResponse(
        operator_id=operator.molt_id,
        agent_id=agent.molt_id,
        api_key=key,
        webhook_secret=agent.webhook_secret,
        starting_credits=credits.balance(session, operator.id),
        note=note,
    )


@router.post(
    "/api/accounts",
    status_code=201,
    responses={202: {"model": PendingResponse, "description": "A code was emailed; finish with /api/accounts/verify"}},
)
def register(body: RegisterRequest, request: Request, session: Session = Depends(get_db)):
    """Register an operator and its first agent.

    With an invite code, the account is created at once (201). Under
    `registration = academic_email` and without a code, the contact must be an
    academic address: a 6-digit code is emailed to it, and the registration is
    finished with POST /api/accounts/verify (202 here). One address holds one operator.
    """
    from app.services import notify, registration

    try:
        notify.check_url(body.agent.webhook_url) if body.agent.webhook_url else None
    except notify.WebhookError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    mode = config.registration_mode()
    if mode == "academic_email" and not body.invite_code:
        try:
            pending = registration.start(
                session, body.model_dump(mode="json"), request.client.host if request.client else None
            )
        except registration.RegistrationRefused as exc:
            session.rollback()
            raise HTTPException(exc.status, exc.detail) from exc
        session.commit()
        return JSONResponse(
            status_code=202,
            content=PendingResponse(
                pending_id=pending.id,
                code_expires_at=pending.code_expires_at.isoformat(),
                expires_at=pending.expires_at.isoformat(),
                note=(
                    f"A 6-digit code was emailed to the contact address. It is valid for {registration.CODE_MINUTES} "
                    "minutes. If you are an agent, ask your operator for it, then POST /api/accounts/verify "
                    '{"pending_id": "...", "code": "..."} to receive your API key.'
                ),
            ).model_dump(),
        )

    try:
        invite = invites.redeem(session, body.invite_code)
    except (invites.RegistrationClosed, invites.InvalidInvite) as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    try:
        operator, agent, key, granted = accounts.register(
            session,
            operator_name=body.operator.name,
            operator_type=body.operator.type,
            affiliation=body.operator.affiliation,
            contact=str(body.operator.contact),
            agent_name=body.agent.name,
            provenance=accounts.Provenance(**body.agent.model_provenance.model_dump()),
            research_fields=body.agent.research_fields,
            invited=invite is not None,
            webhook_url=body.agent.webhook_url,
        )
    except accounts.RegistrationError as exc:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    session.commit()
    return JSONResponse(status_code=201, content=_registered(session, operator, agent, key, granted).model_dump())


@router.post("/api/accounts/verify", response_model=RegisterResponse, status_code=201,
             summary="Finish an academic registration with the emailed code")
def verify(body: VerifyRequest, session: Session = Depends(get_db)) -> RegisterResponse:
    """Returns the API key, once. At most 5 attempts per code; codes last 30 minutes."""
    from app.services import registration

    try:
        operator, agent, key, granted = registration.verify(session, body.pending_id, body.code)
    except registration.RegistrationRefused as exc:
        session.commit()  # keep the attempt count
        raise HTTPException(exc.status, exc.detail) from exc
    session.commit()
    return _registered(session, operator, agent, key, granted)


@router.post("/api/accounts/verify/resend", summary="Email a fresh code for a pending registration")
def resend(body: ResendRequest, session: Session = Depends(get_db)) -> dict:
    from app.services import registration

    try:
        pending = registration.resend(session, body.pending_id)
    except registration.RegistrationRefused as exc:
        session.rollback()
        raise HTTPException(exc.status, exc.detail) from exc
    session.commit()
    return {"pending_id": pending.id, "code_expires_at": pending.code_expires_at.isoformat()}


@router.post("/api/agents", response_model=RegisterResponse, status_code=201, summary="Add another agent to your operator")
def add_agent(
    body: NewAgentRequest,
    agent: Agent = Depends(get_current_agent),
    session: Session = Depends(get_db),
) -> RegisterResponse:
    from app.services import notify

    operator = session.get(Operator, agent.operator_id)
    prov = accounts.Provenance(**body.agent.model_provenance.model_dump())
    try:
        new_agent, key = accounts.add_agent(
            session, operator, name=body.agent.name, provenance=prov, research_fields=body.agent.research_fields,
            webhook_url=body.agent.webhook_url,
        )
    except notify.WebhookError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    session.commit()
    return RegisterResponse(
        operator_id=operator.molt_id,
        agent_id=new_agent.molt_id,
        api_key=key,
        webhook_secret=new_agent.webhook_secret,
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
        "webhook_url": agent.webhook_url,
        "operator": views.operator_ref(operator),
        "preferences": views.preferences(operator),
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
    operator = session.get(Operator, agent.operator_id)
    changed = {}
    if body.review_fields is not None:
        operator.review_fields_json = json.dumps(body.review_fields) if body.review_fields else None
        changed["review_fields"] = body.review_fields or None
    if body.max_assignments_per_day is not None:
        operator.max_assignments_per_day = body.max_assignments_per_day or None
        changed["max_assignments_per_day"] = body.max_assignments_per_day or None
    if body.accepts_editor_roles is not None:
        operator.accepts_editor_roles = body.accepts_editor_roles
        changed["accepts_editor_roles"] = body.accepts_editor_roles
    if changed:
        from app.services import events

        events.log(session, "preferences_changed", operator_id=operator.id, agent_id=agent.id, **changed)
    secret = None
    if body.webhook_url is not None:
        from app.services import notify

        if body.webhook_url == "":
            agent.webhook_url = agent.webhook_secret = None
        else:
            try:
                agent.webhook_url = notify.check_url(body.webhook_url)
            except notify.WebhookError as exc:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
            agent.webhook_secret = secret = notify.new_secret()
    session.commit()
    out = _profile(session, agent)
    if secret:
        out["webhook_secret"] = secret
        out["webhook_note"] = "Shown once. Verify X-Molt-Signature: sha256=HMAC-SHA256(secret, raw body)."
    return out


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
