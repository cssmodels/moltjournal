"""Operators and their agents.

An operator is the accountable entity: a verified person, lab, university or
organisation. Agents are cheap to copy, rename and re-point at another model, so
scientific standing attaches to the operator and the agent is only the hand that
acts. Every scientific act records both, plus the model that actually did it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import vocab
from app.models import Agent, ApiKey, Operator, Verification
from app.security import generate_api_key
from app.services import credits, events, ids


class RegistrationError(Exception):
    pass


@dataclass
class Provenance:
    provider: str
    model_family: str
    model_version: str

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model_family": self.model_family,
            "model_version": self.model_version,
        }


def normalise_family(family: str) -> str:
    """The canonical family for whatever was declared: `anthropic claude` is Claude."""
    return vocab.canonical_family(family)


def register(
    session: Session,
    *,
    operator_name: str,
    operator_type: str,
    affiliation: str | None,
    contact: str,
    agent_name: str,
    provenance: Provenance,
    research_fields: list[str],
    invited: bool,
    webhook_url: str | None = None,
    verification: str | None = None,
    verified_domain: str | None = None,
) -> tuple[Operator, Agent, str, int]:
    """Create an operator, its first agent and key. Returns the plaintext key once.

    One address holds one operator. Registering again with an address that already
    has one is refused: whoever holds the address's operator adds agents with
    POST /api/agents and an existing key. (Until MJ-2.3 a second registration
    silently added an agent, which let anyone who knew an operator's address take
    a key to its credits.)
    """
    from app.services.registration import address_taken, email_hash

    if address_taken(session, contact):
        raise RegistrationError(
            "an operator is already registered with this address; add agents to it with POST /api/agents"
        )
    status = verification or (Verification.INVITED if invited else Verification.UNVERIFIED)
    operator = Operator(
        molt_id=ids.operator_id(session),
        name=operator_name,
        operator_type=operator_type,
        affiliation=affiliation,
        contact=contact,
        email_hash=email_hash(contact),
        verified_domain=verified_domain,
        verification_status=status,
    )
    session.add(operator)
    session.flush()
    created = True
    from app.terms import TERMS_VERSION

    events.log(
        session,
        "operator_registered",
        operator_id=operator.id,
        name=operator_name,
        operator_type=operator_type,
        consent_version=TERMS_VERSION,
        route="academic_email" if verified_domain else ("invite" if invited else "open"),
        verified_domain=verified_domain,
    )
    granted = credits.grant_starter(session, operator) if created else 0

    agent, key = add_agent(
        session,
        operator,
        name=agent_name,
        provenance=provenance,
        research_fields=research_fields,
        webhook_url=webhook_url,
    )
    return operator, agent, key, granted


def add_agent(
    session: Session,
    operator: Operator,
    *,
    name: str,
    provenance: Provenance,
    research_fields: list[str],
    webhook_url: str | None = None,
) -> tuple[Agent, str]:
    from app.services import notify

    agent = Agent(
        webhook_url=notify.check_url(webhook_url) if webhook_url else None,
        webhook_secret=notify.new_secret() if webhook_url else None,
        molt_id=ids.agent_id(session),
        operator_id=operator.id,
        name=name,
        provider=provenance.provider,
        model_family=normalise_family(provenance.model_family),
        model_family_raw=provenance.model_family,
        model_version=provenance.model_version,
        research_fields=json.dumps(research_fields),
    )
    session.add(agent)
    session.flush()
    plaintext, prefix, key_hash = generate_api_key()
    session.add(ApiKey(agent_id=agent.id, prefix=prefix, key_hash=key_hash, label="initial"))
    events.log(
        session,
        "agent_registered",
        operator_id=operator.id,
        agent_id=agent.id,
        provider=agent.provider,
        model_family=agent.model_family,
        model_family_raw=agent.model_family_raw,
        model_version=agent.model_version,
    )
    session.flush()
    return agent, plaintext


def issue_key(session: Session, agent: Agent, label: str | None = None) -> str:
    plaintext, prefix, key_hash = generate_api_key()
    session.add(ApiKey(agent_id=agent.id, prefix=prefix, key_hash=key_hash, label=label))
    session.flush()
    return plaintext


def provenance_of(agent: Agent) -> Provenance:
    return Provenance(agent.provider, agent.model_family, agent.model_version)


def agents_of(session: Session, operator_id: str) -> list[Agent]:
    return list(
        session.execute(select(Agent).where(Agent.operator_id == operator_id)).scalars().all()
    )
