from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.domain.auth import Role
from app.domain.retrieval import RetrievalMode

CITATION_ID_PATTERN = r"^K(?:[1-9]|[1-9][0-9])$"


class EvidenceRelation(StrEnum):
    DIRECT = "direct"
    RELATED = "related"
    IRRELEVANT = "irrelevant"


class DraftDisposition(StrEnum):
    ANSWERED = "answered"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class KnowledgeAnswerStatus(StrEnum):
    PROCESSING = "processing"
    ANSWERED = "answered"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    EVIDENCE_CONFLICT = "evidence_conflict"
    SECURITY_BLOCKED = "security_blocked"
    FAILED = "failed"


class GroundedAnswerQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=2, max_length=2_000, repr=False)
    as_of: datetime
    retrieval_mode: RetrievalMode = RetrievalMode.HYBRID
    candidate_k: int = Field(default=10, ge=1, le=20)
    max_evidence: int = Field(default=5, ge=1, le=10)
    request_id: str = Field(min_length=1, max_length=64)
    trace_id: str = Field(min_length=1, max_length=64)

    @field_validator("as_of")
    @classmethod
    def as_of_must_have_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Grounded answer as_of must have a timezone.")
        return value.astimezone(UTC)


class EvidenceAssessmentItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(pattern=CITATION_ID_PATTERN)
    relevance: int = Field(ge=0, le=100)
    relation: EvidenceRelation


class EvidenceConflictPair(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    left_evidence_id: str = Field(pattern=CITATION_ID_PATTERN)
    right_evidence_id: str = Field(pattern=CITATION_ID_PATTERN)
    explanation: str = Field(min_length=3, max_length=300, repr=False)

    @model_validator(mode="after")
    def evidence_ids_must_differ(self) -> "EvidenceConflictPair":
        if self.left_evidence_id == self.right_evidence_id:
            raise ValueError("A conflict pair must contain two evidence items.")
        return self


class EvidenceRerankOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    assessments: tuple[EvidenceAssessmentItem, ...]
    conflicts: tuple[EvidenceConflictPair, ...] = ()


class DraftClaimSupport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(pattern=CITATION_ID_PATTERN)
    exact_quote: str = Field(min_length=1, max_length=1_000, repr=False)


class DraftGroundedClaim(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=2, max_length=500)
    supports: tuple[DraftClaimSupport, ...] = Field(min_length=1)


class GroundedAnswerDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    disposition: DraftDisposition
    claims: tuple[DraftGroundedClaim, ...]

    @model_validator(mode="after")
    def disposition_must_match_claims(self) -> "GroundedAnswerDraft":
        if (
            self.disposition is DraftDisposition.ANSWERED
            and not self.claims
        ):
            raise ValueError("An answered draft must contain claims.")
        if (
            self.disposition is DraftDisposition.INSUFFICIENT_EVIDENCE
            and self.claims
        ):
            raise ValueError("An insufficient draft cannot contain claims.")
        return self


class GroundedCitation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    citation_id: str = Field(pattern=CITATION_ID_PATTERN)
    chunk_id: UUID
    document_id: UUID
    source_key: str
    title: str
    source_uri: str
    document_version: str
    source_line_start: int = Field(ge=1)
    source_line_end: int = Field(ge=1)
    exact_quote: str = Field(min_length=1, max_length=1_000, repr=False)


class GroundedClaim(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    citation_ids: tuple[str, ...]


class KnowledgeAnswerRun(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    tenant_id: UUID
    actor_id: str
    actor_roles: tuple[Role, ...]
    query_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    retrieval_run_id: UUID | None
    status: KnowledgeAnswerStatus
    retrieved_candidate_count: int = Field(ge=0)
    eligible_candidate_count: int = Field(ge=0)
    deduplicated_candidate_count: int = Field(ge=0)
    selected_candidate_count: int = Field(ge=0)
    conflict_count: int = Field(ge=0)
    citation_count: int = Field(ge=0)
    rerank_call_id: UUID | None
    answer_call_id: UUID | None
    answer_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    error_code: str | None
    latency_ms: float = Field(ge=0)
    request_id: str
    trace_id: str
    created_at: datetime
    completed_at: datetime | None


class KnowledgeAnswerCitationTrace(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    citation_id: str = Field(pattern=CITATION_ID_PATTERN)
    chunk_id: UUID
    source_key: str
    source_uri: str
    document_version: str
    source_line_start: int = Field(ge=1)
    source_line_end: int = Field(ge=1)
    quote_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    claim_indexes: tuple[int, ...]


class GroundedAnswerResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run: KnowledgeAnswerRun
    status: KnowledgeAnswerStatus
    answer_text: str
    claims: tuple[GroundedClaim, ...]
    citations: tuple[GroundedCitation, ...]
