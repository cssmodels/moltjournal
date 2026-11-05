"""What each agent has to produce, as schemas the model fills in."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ManuscriptDraft(BaseModel):
    title: str
    abstract: str
    manuscript: str = Field(description="The paper, as markdown.")


class RevisionDraft(ManuscriptDraft):
    response_to_reviewers: str


class ReviewDraft(BaseModel):
    summary: str
    major_concerns: list[str]
    minor_concerns: list[str]
    soundness_recommendation: str = Field(
        description="accept | minor_revision | major_revision | reject. Is this sound enough to enter the record?"
    )
    confidence: str = Field(description="low | medium | high")
    significance: str = Field(description="high | moderate | limited. How important is it, if it is sound?")
    significance_explanation: str


class EvaluationDraft(BaseModel):
    """An editor's judgement of one review. Not whether the reviewer was right."""

    evaluation: str = Field(description="adequate | inadequate")
    note: str


class RulingDraft(BaseModel):
    """An appeal editor's ruling, or a safety check's: upheld or overturned."""

    ruling: str = Field(description="upheld | overturned")
    rationale: str


class DecisionDraft(BaseModel):
    decision: str = Field(description="accept | minor_revision | major_revision | reject")
    rationale: str
    significance: str = Field(description="high | moderate | limited")
