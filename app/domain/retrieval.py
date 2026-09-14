from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.auth import Role


class RetrievalMode(StrEnum):
    SEMANTIC = "semantic"
    KEYWORD = "keyword"
    HYBRID = "hybrid"


class KnowledgeSearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=2, max_length=2_000, repr=False)
    as_of: datetime
    top_k: int = Field(default=5, ge=1, le=20)
    mode: RetrievalMode = RetrievalMode.HYBRID
    request_id: str = Field(min_length=1, max_length=64)
    trace_id: str = Field(min_length=1, max_length=64)

    @field_validator("as_of")
    @classmethod
    def as_of_must_have_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Knowledge search as_of must have a timezone.")
        return value.astimezone(UTC)


class RewrittenKnowledgeQuery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    semantic_text: str = Field(min_length=2, max_length=2_000, repr=False)
    keyword_terms: tuple[str, ...]
    strategy: str = Field(min_length=1, max_length=64)


class ChunkEmbeddingUpdate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: UUID
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    embedding: tuple[float, ...]
    embedding_model: str = Field(min_length=1, max_length=100)
    embedded_at: datetime


class KnowledgeRetrievalCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: UUID
    document_id: UUID
    tenant_id: UUID
    source_key: str
    title: str
    content: str = Field(repr=False)
    source_uri: str
    document_version: str
    source_line_start: int
    source_line_end: int
    effective_from: datetime
    effective_to: datetime | None
    allowed_roles: tuple[Role, ...]
    vector_distance: float | None = None
    keyword_score: float | None = None


class KnowledgeSearchHit(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: UUID
    document_id: UUID
    source_key: str
    title: str
    content: str = Field(repr=False)
    source_uri: str
    document_version: str
    source_line_start: int
    source_line_end: int
    effective_from: datetime
    effective_to: datetime | None
    fused_score: float
    semantic_rank: int | None
    keyword_rank: int | None
    vector_distance: float | None
    keyword_score: float | None


class KnowledgeRetrievalRun(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    tenant_id: UUID
    actor_id: str
    actor_roles: tuple[Role, ...]
    query_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    rewritten_query_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    rewrite_strategy: str
    search_mode: RetrievalMode
    embedding_model: str
    top_k: int
    semantic_candidate_count: int
    keyword_candidate_count: int
    result_count: int
    degraded_reason: str | None
    latency_ms: float
    request_id: str
    trace_id: str
    created_at: datetime


class KnowledgeRetrievalHitTrace(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    rank: int
    chunk_id: UUID
    semantic_rank: int | None
    keyword_rank: int | None
    fused_score: float
    vector_distance: float | None
    keyword_score: float | None


class KnowledgeSearchResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run: KnowledgeRetrievalRun
    rewritten_query: RewrittenKnowledgeQuery
    hits: tuple[KnowledgeSearchHit, ...]


class KnowledgeIndexingResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    document_id: UUID
    embedding_model: str
    indexed_chunk_count: int

