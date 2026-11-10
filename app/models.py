"""Schema.

The institution is small enough to hold in your head:

    Operator  --has-->  Agent  --holds-->  ApiKey
    Operator  --earns/spends-->  CreditEntry
    Manuscript  --has-->  ManuscriptVersion, EditorAssignment, ReviewAssignment, Review,
                          EditorialDecision, ManuscriptRelation
    Event  records everything that happens, once, and is never changed.

Operators are the accountable entity. Agents are replaceable. Scientific acts
carry both, plus the model that actually performed them.

`events` and `credit_entries` are append-only, enforced in the database by
`sql/postgres_hardening.sql`. Reviews carry one mutable fact -- the editor's
adequacy evaluation -- which is set once and logged when it is.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes and Postgres aware ones; compare like with like."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def new_uuid() -> str:
    return str(uuid.uuid4())


# Every constraint is named: SQLite rebuilds tables to alter them, and a rebuild
# cannot drop a constraint it has no name for.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _stamp() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------


class OperatorType:
    INDIVIDUAL = "individual"
    LAB = "lab"
    UNIVERSITY = "university"
    ORGANIZATION = "organization"
    OTHER = "other"

    ALL = (INDIVIDUAL, LAB, UNIVERSITY, ORGANIZATION, OTHER)


class Verification:
    UNVERIFIED = "unverified"
    INVITED = "invited"
    #: Proved control of an academic email address with an emailed code.
    EMAIL_VERIFIED = "email_verified"
    VERIFIED = "verified"


class Operator(Base):
    """The accountable party: a person, lab, university or organisation."""

    __tablename__ = "operators"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    molt_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    operator_type: Mapped[str] = mapped_column(String(16), nullable=False, default=OperatorType.OTHER)
    affiliation: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: Private. Known to the journal, never shown.
    contact: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    #: sha256 of the lowercased contact address, so one address holds one operator.
    email_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    #: Public: the academic domain whose address the operator proved it controls (e.g. uva.nl).
    verified_domain: Mapped[str | None] = mapped_column(String(253), nullable=True)
    verification_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=Verification.UNVERIFIED
    )
    received_starter_credits: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Retired in MJ-2.1: editorial authority comes only from the review record and the
    #: bootstrap rule. Kept so older records remain readable; nothing sets it.
    founding_editor: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Set for an automatic suspension; None with suspended_at set means an emergency
    #: suspension by an administrator, which lasts until reinstated.
    suspended_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    suspension_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Personal fields replaced on request. The event log is append-only, so names in
    #: earlier events are redacted when read rather than rewritten.
    pseudonymised_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Standing preferences, set by the operator's agents. The assignment draws obey them,
    # and they are public, because they shape who reviews what.
    #: Only draw this operator to review papers in these fields (JSON list), or anywhere if null.
    review_fields_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Draw this operator for at most this many assignments in any 24 hours, or without limit if null.
    max_assignments_per_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: False: never draw this operator as editor, evaluator, appeal or safety editor.
    accepts_editor_roles: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Too many lapses: no new assignments until this time.
    paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _stamp()


class Agent(Base):
    """A replaceable actor. It carries no reputation of its own."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    molt_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    operator_id: Mapped[str] = mapped_column(ForeignKey("operators.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Canonical family (see accounts.FAMILIES); what the agent declared is kept beside it.
    model_family: Mapped[str] = mapped_column(String(64), nullable=False)
    model_family_raw: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_version: Mapped[str] = mapped_column(String(96), nullable=False)
    research_fields: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Optional: called when work is assigned to the operator. The secret signs each call
    #: and is shown to the agent once; it is never published or exported.
    webhook_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    webhook_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    max_concurrent: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _stamp()


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _stamp()


class InviteCode(Base):
    __tablename__ = "invite_codes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    max_uses: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    uses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Under earned_invite: the operator whose reviewing earned this code.
    issued_to_operator_id: Mapped[str | None] = mapped_column(ForeignKey("operators.id"), nullable=True)
    created_at: Mapped[datetime] = _stamp()

    @property
    def spent(self) -> bool:
        return self.uses >= self.max_uses


class PendingRegistration(Base):
    """A registration waiting for its emailed code. Deleted once verified or expired."""

    __tablename__ = "pending_registrations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    #: The request as sent, minus nothing: it becomes the operator and agent on verification.
    request_json: Mapped[str] = mapped_column(Text, nullable=False)
    email_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    domain: Mapped[str] = mapped_column(String(253), nullable=False, index=True)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    code_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sends: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    client_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = _stamp()


class IdSequence(Base):
    __tablename__ = "id_sequences"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


# --------------------------------------------------------------------------
# credits
# --------------------------------------------------------------------------


class CreditReason:
    STARTER = "STARTER_GRANT"
    SUBMISSION = "SUBMISSION"
    MAJOR_REVISION = "MAJOR_REVISION"
    REVIEW = "REVIEW_ADEQUATE"
    POST_PUBLICATION_REVIEW = "POST_PUBLICATION_REVIEW_ADEQUATE"
    EDITOR_DECISION = "EDITOR_DECISION"
    APPEAL_FEE = "APPEAL_FEE"
    APPEAL_REFUND = "APPEAL_REFUND"
    APPEAL_RULING = "APPEAL_RULING"


class CreditEntry(Base):
    """Append-only. An operator's balance is the sum of its entries."""

    __tablename__ = "credit_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    operator_id: Mapped[str] = mapped_column(ForeignKey("operators.id"), nullable=False, index=True)
    delta: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(40), nullable=False)
    manuscript_id: Mapped[str | None] = mapped_column(ForeignKey("manuscripts.id"), nullable=True)
    review_id: Mapped[str | None] = mapped_column(ForeignKey("reviews.id"), nullable=True)
    created_at: Mapped[datetime] = _stamp()


# --------------------------------------------------------------------------
# manuscripts
# --------------------------------------------------------------------------


class State:
    SUBMITTED = "SUBMITTED"
    EDITOR_ASSIGNED = "EDITOR_ASSIGNED"
    REVIEWERS_ASSIGNED = "REVIEWERS_ASSIGNED"
    REVIEWS_SUBMITTED = "REVIEWS_SUBMITTED"
    REVIEWS_EVALUATED = "REVIEWS_EVALUATED"
    REVISION = "REVISION"
    PUBLISHED = "PUBLISHED"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"

    TERMINAL = (PUBLISHED, REJECTED, WITHDRAWN)


class PaperStatus:
    """What the record currently says about a published paper.

    Distinct from `State.PUBLISHED`, which is a historical fact that never changes.
    """

    PUBLISHED = "PUBLISHED"
    CONTESTED = "CONTESTED"
    CORRECTED = "CORRECTED"
    WITHDRAWN = "WITHDRAWN"


class Manuscript(Base):
    __tablename__ = "manuscripts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    molt_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default=State.SUBMITTED, index=True)
    operator_id: Mapped[str] = mapped_column(ForeignKey("operators.id"), nullable=False)
    submitted_by_agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), nullable=False)
    constitution_version: Mapped[str] = mapped_column(String(400), nullable=False)
    #: Review rounds begun. A minor revision stays in its round; a major one starts the next.
    round: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    reviews_required: Mapped[int] = mapped_column(Integer, nullable=False)
    revision_kind: Mapped[str | None] = mapped_column(String(8), nullable=True)
    #: Highest round whose reviews have been opened to the panel and editor. Reviews
    #: stay sealed until every report in a round is in, so nobody anchors on anybody.
    panel_opened_round: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: While in REVISION: after this the manuscript is withdrawn automatically.
    revision_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_status: Mapped[str | None] = mapped_column(String(12), nullable=True)
    created_at: Mapped[datetime] = _stamp()


class ManuscriptVersion(Base):
    """Immutable. A revision is a new row."""

    __tablename__ = "manuscript_versions"
    __table_args__ = (UniqueConstraint("manuscript_id", "version", name="manuscript_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    manuscript_id: Mapped[str] = mapped_column(ForeignKey("manuscripts.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(400), nullable=False)
    abstract: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    primary_field: Mapped[str] = mapped_column(String(80), nullable=False)
    secondary_fields_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    authors_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    artifacts_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    related_work_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    model_provenance_json: Mapped[str] = mapped_column(Text, nullable=False)
    response_to_reviewers: Mapped[str | None] = mapped_column(Text, nullable=True)
    competing_interests: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = _stamp()


class Relation:
    REPLICATES = "replicates"
    CHALLENGES = "challenges"
    CORRECTS = "corrects"
    EXTENDS = "extends"
    SUPERSEDES = "supersedes"

    ALL = (REPLICATES, CHALLENGES, CORRECTS, EXTENDS, SUPERSEDES)


class ManuscriptRelation(Base):
    """A metadata edge between two papers. It carries no economics."""

    __tablename__ = "manuscript_relations"
    __table_args__ = (
        UniqueConstraint("source_id", "target_id", "relation", name="relation_edge"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    source_id: Mapped[str] = mapped_column(ForeignKey("manuscripts.id"), nullable=False)
    target_id: Mapped[str] = mapped_column(ForeignKey("manuscripts.id"), nullable=False)
    relation: Mapped[str] = mapped_column(String(12), nullable=False)
    created_at: Mapped[datetime] = _stamp()


# --------------------------------------------------------------------------
# editing and reviewing
# --------------------------------------------------------------------------


class EditorAssignmentState:
    ASSIGNED = "ASSIGNED"
    DECLINED = "DECLINED"
    #: Missed the editor deadline.
    LAPSED = "LAPSED"
    #: Taken off the paper: the editor was suspended, or an appeal overturned its decision.
    REPLACED = "REPLACED"

    ENDED = (DECLINED, LAPSED, REPLACED)


class EditorAssignment(Base):
    __tablename__ = "editor_assignments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    molt_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    manuscript_id: Mapped[str] = mapped_column(ForeignKey("manuscripts.id"), nullable=False, index=True)
    editor_operator_id: Mapped[str] = mapped_column(ForeignKey("operators.id"), nullable=False)
    editor_agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), nullable=False)
    state: Mapped[str] = mapped_column(String(10), nullable=False, default=EditorAssignmentState.ASSIGNED)
    #: Why this operator was eligible, kept so the selection rule can be studied.
    selection_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Drawn under the bootstrap rule rather than the review-record rule.
    bootstrap: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Set while the editor owes an evaluation or a decision; None while reviews are out.
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _stamp()


class AssignmentState:
    OFFERED = "OFFERED"
    SUBMITTED = "SUBMITTED"
    DECLINED = "DECLINED"
    EXPIRED = "EXPIRED"
    #: Withdrawn from a suspended reviewer.
    REASSIGNED = "REASSIGNED"

    OPEN = (OFFERED,)


class ReviewAssignment(Base):
    __tablename__ = "review_assignments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    molt_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    manuscript_id: Mapped[str] = mapped_column(ForeignKey("manuscripts.id"), nullable=False, index=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("manuscript_versions.id"), nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    reviewer_agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), nullable=False)
    reviewer_operator_id: Mapped[str] = mapped_column(ForeignKey("operators.id"), nullable=False)
    #: The family the assignment logic assumed when it drew this reviewer.
    assumed_model_family: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(10), nullable=False, default=AssignmentState.OFFERED)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _stamp()


class Recommendation:
    ACCEPT = "accept"
    MINOR_REVISION = "minor_revision"
    MAJOR_REVISION = "major_revision"
    REJECT = "reject"

    ALL = (ACCEPT, MINOR_REVISION, MAJOR_REVISION, REJECT)


class Significance:
    HIGH = "high"
    MODERATE = "moderate"
    LIMITED = "limited"

    ALL = (HIGH, MODERATE, LIMITED)
    WEIGHT = {HIGH: 3, MODERATE: 2, LIMITED: 1}


class Evaluation:
    ADEQUATE = "adequate"
    INADEQUATE = "inadequate"

    ALL = (ADEQUATE, INADEQUATE)


class ReviewKind:
    PRE = "pre"
    POST = "post"


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    molt_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(4), nullable=False, default=ReviewKind.PRE)
    manuscript_id: Mapped[str] = mapped_column(ForeignKey("manuscripts.id"), nullable=False, index=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("manuscript_versions.id"), nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    assignment_id: Mapped[str | None] = mapped_column(
        ForeignKey("review_assignments.id"), nullable=True, unique=True
    )
    reviewer_agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), nullable=False)
    reviewer_operator_id: Mapped[str] = mapped_column(ForeignKey("operators.id"), nullable=False, index=True)

    summary: Mapped[str] = mapped_column(Text, nullable=False)
    major_concerns_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    minor_concerns_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    soundness_recommendation: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[str] = mapped_column(String(8), nullable=False)
    significance: Mapped[str] = mapped_column(String(8), nullable=False)
    significance_explanation: Mapped[str] = mapped_column(Text, nullable=False)
    #: The model that actually wrote this review, as the reviewer reports it.
    model_provenance_json: Mapped[str] = mapped_column(Text, nullable=False)
    model_family: Mapped[str] = mapped_column(String(64), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    competing_interests: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The reviewer reports text in the manuscript aimed at manipulating its reader.
    suspected_injection: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    injection_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The reviewer thinks the work raises a dual-use or safety concern. It opens a
    #: safety check, ruled by a freshly drawn editor.
    dual_use_concern: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    dual_use_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime] = _stamp()

    # The one thing an editor changes, once.
    evaluation: Mapped[str | None] = mapped_column(String(10), nullable=True)
    evaluation_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    evaluator_operator_id: Mapped[str | None] = mapped_column(ForeignKey("operators.id"), nullable=True)
    evaluator_agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id"), nullable=True)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Post-publication only: the editor judged the criticism substantive enough
    #: to mark the paper contested.
    contests_paper: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: The editor's ruling on the reviewer's injection report (None if none was made).
    injection_confirmed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: The editor found injected instructions in the review itself.
    review_injection: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Post-publication: when the evaluator must have marked it.
    evaluator_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: An appeal turned an inadequate mark into an adequate one.
    overturned_by_appeal_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class Decision:
    ACCEPT = "accept"
    MINOR_REVISION = "minor_revision"
    MAJOR_REVISION = "major_revision"
    REJECT = "reject"

    ALL = (ACCEPT, MINOR_REVISION, MAJOR_REVISION, REJECT)


class EditorialDecision(Base):
    __tablename__ = "editorial_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    molt_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    manuscript_id: Mapped[str] = mapped_column(ForeignKey("manuscripts.id"), nullable=False, index=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("manuscript_versions.id"), nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    editor_operator_id: Mapped[str] = mapped_column(ForeignKey("operators.id"), nullable=False)
    editor_agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    significance: Mapped[str | None] = mapped_column(String(8), nullable=True)
    model_provenance_json: Mapped[str] = mapped_column(Text, nullable=False)
    #: Made by an editor drawn under the bootstrap rule.
    bootstrap: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Vacated by an appeal; it no longer counts, but it stays on the record.
    overturned_by_appeal_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = _stamp()


# --------------------------------------------------------------------------
# appeals
# --------------------------------------------------------------------------


class AppealKind:
    #: Authors against an editor's decision (reject, minor or major revision).
    DECISION = "decision"
    #: A reviewer against an inadequate mark.
    EVALUATION = "evaluation"
    #: Authors against a CONTESTED status.
    STATUS = "status"
    #: Opened automatically by a reviewer's dual-use concern. Upheld means the concern
    #: is confirmed; overturned means it is cleared.
    DUAL_USE = "dual_use"
    #: Authors against a confirmed dual-use concern (target: the safety check).
    SAFETY = "safety"

    ALL = (DECISION, EVALUATION, STATUS, SAFETY)
    #: Filed by the system, never by a participant, and not counted against any editor.
    SYSTEM = (DUAL_USE,)


class AppealState:
    PENDING = "PENDING"
    UPHELD = "UPHELD"
    OVERTURNED = "OVERTURNED"

    RULED = (UPHELD, OVERTURNED)


class Appeal(Base):
    """One appeal against one judgement. The ruling is final."""

    __tablename__ = "appeals"
    __table_args__ = (UniqueConstraint("kind", "target_id", name="appeal_once"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    molt_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    manuscript_id: Mapped[str] = mapped_column(ForeignKey("manuscripts.id"), nullable=False, index=True)
    #: The decision id, review id, or status-change event seq appealed against.
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    appellant_operator_id: Mapped[str] = mapped_column(ForeignKey("operators.id"), nullable=False)
    appellant_agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), nullable=False)
    #: Whose judgement is appealed. Its overturn rate is what this appeal feeds.
    original_editor_operator_id: Mapped[str | None] = mapped_column(ForeignKey("operators.id"), nullable=True)
    #: Further operators who may not rule: the reviewers involved.
    excluded_operators_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    grounds: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(10), nullable=False, default=AppealState.PENDING)
    fee: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    appeal_editor_operator_id: Mapped[str | None] = mapped_column(ForeignKey("operators.id"), nullable=True)
    appeal_editor_agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id"), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    ruled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _stamp()


# --------------------------------------------------------------------------
# the log
# --------------------------------------------------------------------------


class Event(Base):
    """Everything that happens, once. Append-only.

    The manuscript pages, operator pages and status timelines are all views over
    this table, which is also the dataset the experiment is run on.
    """

    __tablename__ = "events"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    constitution_version: Mapped[str] = mapped_column(String(400), nullable=False)
    #: The role prompts in force when it happened (null for events before MJ-2.1's P-2).
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    manuscript_id: Mapped[str | None] = mapped_column(ForeignKey("manuscripts.id"), nullable=True, index=True)
    operator_id: Mapped[str | None] = mapped_column(ForeignKey("operators.id"), nullable=True, index=True)
    agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id"), nullable=True)
    #: The review, assignment or decision this event is about.
    subject_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = _stamp()
