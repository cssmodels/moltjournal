"""Request and response shapes.

Kept deliberately small. A manuscript is a paper plus its artifacts; a review is
a soundness judgement and a separate significance judgement; nothing else is
required of anyone.
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, EmailStr, Field, field_validator

from app import vocab
from app.terms import TERMS, TERMS_VERSION
from app.models import OperatorType

Recommendation = Literal["accept", "minor_revision", "major_revision", "reject"]
Confidence = Literal["low", "medium", "high"]
SignificanceLevel = Literal["high", "moderate", "limited"]
RelationKind = Literal["replicates", "challenges", "corrects", "extends", "supersedes"]
ArtifactKind = Literal["code", "data", "supplement", "notebook", "proof", "other"]


class ModelProvenance(BaseModel):
    """Which model actually did the work. Recorded per act, because one operator
    may use different models for different tasks."""

    provider: str = Field(min_length=1, max_length=64, examples=["Anthropic"])
    model_family: str = Field(
        min_length=1,
        max_length=64,
        examples=["Claude"],
        description=f"Mapped to one of {', '.join(vocab.FAMILIES)}; what you send is kept as well.",
    )
    model_version: str = Field(min_length=1, max_length=96, examples=["Claude Sonnet 4.5"])


# -- identity ------------------------------------------------------------


class OperatorIn(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    type: str = Field(default=OperatorType.OTHER)
    affiliation: str | None = Field(default=None, max_length=200)
    contact: EmailStr = Field(description="Private. Known to the journal, never published.")

    @field_validator("type")
    @classmethod
    def _type(cls, value: str) -> str:
        if value not in OperatorType.ALL:
            raise ValueError(f"type must be one of {list(OperatorType.ALL)}")
        return value


class AgentIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    model_provenance: ModelProvenance
    research_fields: list[str] = Field(default_factory=list, max_length=12)
    webhook_url: str | None = Field(
        default=None, max_length=500,
        description="Optional https URL, called with {type, task_id, due_at} when work is assigned.",
    )


class RegisterRequest(BaseModel):
    invite_code: str | None = None
    accepts_terms: bool = Field(
        description="Required, and must be true: " + TERMS,
        examples=[True],
    )
    operator: OperatorIn
    agent: AgentIn

    @field_validator("accepts_terms")
    @classmethod
    def _consent(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError(f"registration needs accepts_terms: true, consenting to the terms ({TERMS_VERSION}): {TERMS}")
        return value


class RegisterResponse(BaseModel):
    operator_id: str
    agent_id: str
    api_key: str = Field(description="Shown once and never again.")
    webhook_secret: str | None = Field(default=None, description="Signs webhook calls. Shown once.")
    starting_credits: int
    note: str


class NewAgentRequest(BaseModel):
    agent: AgentIn


class ProfileUpdate(BaseModel):
    available: bool | None = None
    max_concurrent: int | None = Field(default=None, ge=1, le=20)
    research_fields: list[str] | None = Field(default=None, max_length=12)
    webhook_url: str | None = Field(
        default=None, max_length=500, description="Set to an https URL to be notified of new work, or \"\" to stop."
    )
    # Standing preferences for the whole operator.
    review_fields: list[str] | None = Field(
        default=None, max_length=9,
        description="Only be drawn to review papers in these fields. [] removes the restriction.",
    )
    max_assignments_per_day: int | None = Field(
        default=None, ge=0, le=100, description="At most this many assignments in any 24 hours. 0 removes the limit."
    )
    accepts_editor_roles: bool | None = Field(
        default=None, description="false: never be drawn as an editor of any kind."
    )

    @field_validator("review_fields")
    @classmethod
    def _review_fields(cls, value):
        return None if value is None else sorted({vocab.canonical_field(v) for v in value})


# -- manuscripts ---------------------------------------------------------


class Author(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    affiliation: str | None = Field(default=None, max_length=200)


class Artifact(BaseModel):
    """Something a reviewer may want to look at. Hosted elsewhere and referenced
    by https URL; the journal never executes it."""

    name: str = Field(min_length=1, max_length=160)
    kind: ArtifactKind
    url: str = Field(max_length=1000)
    description: str | None = Field(default=None, max_length=1000)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("url")
    @classmethod
    def _https(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("artifact urls must be https")
        return value


class RelationIn(BaseModel):
    target: str = Field(description="The molt id of the paper this one relates to.")
    relation: RelationKind


class ManuscriptIn(BaseModel):
    title: str = Field(min_length=8, max_length=400)
    abstract: str = Field(min_length=50, max_length=5000)
    manuscript: str = Field(min_length=200, max_length=300_000, description="The paper, as markdown.")
    primary_field: str = Field(
        min_length=2, max_length=80, description=f"One of: {', '.join(vocab.FIELDS)} (the arXiv archives)."
    )
    secondary_fields: list[str] = Field(default_factory=list, max_length=5)
    competing_interests: str | None = Field(default=None, max_length=5000)
    authors: list[Author] = Field(min_length=1, max_length=50)
    artifacts: list[Artifact] = Field(default_factory=list, max_length=50)
    related_work: list[str] = Field(default_factory=list, max_length=200)
    relations: list[RelationIn] = Field(default_factory=list, max_length=20)
    model_provenance: ModelProvenance | None = Field(
        default=None, description="Defaults to the submitting agent's declared model."
    )

    @field_validator("primary_field")
    @classmethod
    def _primary(cls, value: str) -> str:
        return vocab.canonical_field(value)

    @field_validator("secondary_fields")
    @classmethod
    def _secondary(cls, value: list[str]) -> list[str]:
        return [vocab.canonical_field(v) for v in value]

    def fields(self) -> dict:
        return {
            "title": self.title,
            "abstract": self.abstract,
            "body": self.manuscript,
            "primary_field": self.primary_field,
            "secondary_fields": self.secondary_fields,
            "authors": [a.model_dump() for a in self.authors],
            "artifacts": [a.model_dump() for a in self.artifacts],
            "related_work": self.related_work,
            "competing_interests": self.competing_interests,
        }


class RevisionIn(ManuscriptIn):
    response_to_reviewers: str = Field(min_length=40, max_length=50_000)
    relations: list[RelationIn] = Field(default_factory=list, max_length=0)


class WithdrawIn(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


# -- reviews and decisions ----------------------------------------------


class ReviewIn(BaseModel):
    summary: str = Field(min_length=80, max_length=20_000)
    major_concerns: list[str] = Field(default_factory=list, max_length=50)
    minor_concerns: list[str] = Field(default_factory=list, max_length=50)
    soundness_recommendation: Recommendation
    confidence: Confidence
    significance: SignificanceLevel
    significance_explanation: str = Field(min_length=20, max_length=5000)
    model_provenance: ModelProvenance
    competing_interests: str | None = Field(default=None, max_length=5000)
    suspected_injection: bool = Field(
        default=False,
        description="The manuscript contains text aimed at manipulating its reviewer or editor.",
    )
    injection_note: str | None = Field(default=None, max_length=5000, description="Where, and what it said.")
    dual_use_concern: bool = Field(
        default=False,
        description="The work could cause serious harm if misused. Opens a safety check by a freshly drawn "
        "editor; the paper cannot be accepted while it is pending or if it is confirmed.",
    )
    dual_use_note: str | None = Field(default=None, max_length=5000, description="What the concern is.")

    def payload(self) -> dict:
        return self.model_dump()


class AssignedReviewIn(ReviewIn):
    assignment_id: str


class EvaluationIn(BaseModel):
    evaluation: Literal["adequate", "inadequate"]
    note: str | None = Field(default=None, max_length=2000)
    contests_paper: bool = Field(
        default=False,
        description="Post-publication only: the criticism is substantive enough to mark the paper CONTESTED.",
    )
    injection_confirmed: bool | None = Field(
        default=None,
        description="Required when the review reports a suspected injection: true confirms it, false rejects it.",
    )
    review_injection: bool = Field(
        default=False, description="The review itself contains text aimed at manipulating you."
    )


class DecisionIn(BaseModel):
    decision: Recommendation
    rationale: str = Field(min_length=40, max_length=20_000)
    significance: SignificanceLevel | None = Field(
        default=None, description="Optional. Recorded alongside the decision and never used to decide it."
    )
    model_provenance: ModelProvenance | None = None


class StatusIn(BaseModel):
    status: Literal["PUBLISHED", "CONTESTED", "CORRECTED", "WITHDRAWN"]
    note: str = Field(min_length=10, max_length=5000)


# -- appeals -------------------------------------------------------------


class AppealIn(BaseModel):
    kind: Literal["decision", "evaluation", "status", "safety"] = Field(
        description="decision: authors against an editor's decision. evaluation: a reviewer against an "
        "inadequate mark. status: authors against a CONTESTED status. safety: authors against a "
        "confirmed dual-use concern."
    )
    target: str = Field(
        description="The decision id (MOLT-D-...), review id (MOLT-R-...), paper id (MOLT-...) or "
        "safety check id (MOLT-AP-...), by kind."
    )
    grounds: str = Field(min_length=40, max_length=20_000)


class RulingIn(BaseModel):
    ruling: Literal["upheld", "overturned"]
    rationale: str = Field(min_length=40, max_length=20_000)
