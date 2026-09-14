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

DEFAULT_KNOWLEDGE_ROLES = (
    Role.AGENT,
    Role.SUPERVISOR,
    Role.TENANT_ADMIN,
)


class KnowledgeIngestionStatus(StrEnum):
    PROCESSING = "processing"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


class IngestPolicyDocumentCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID
    source_key: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$",
    )
    document_version: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    title: str = Field(min_length=3, max_length=200)
    source_uri: str = Field(min_length=3, max_length=500)
    media_type: str = Field(default="text/markdown", pattern=r"^text/markdown$")
    source_text: str = Field(
        min_length=1,
        max_length=1_000_000,
        repr=False,
    )
    effective_from: datetime
    effective_to: datetime | None = None
    idempotency_key: str = Field(min_length=8, max_length=128)
    request_id: str = Field(min_length=1, max_length=64)
    trace_id: str = Field(min_length=1, max_length=64)
    max_attempts: int = Field(default=3, ge=1, le=10)
    allowed_roles: tuple[Role, ...] = DEFAULT_KNOWLEDGE_ROLES

    @field_validator("effective_from", "effective_to")
    @classmethod
    def timestamps_must_have_timezone(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Knowledge timestamps must have a timezone.")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_business_identity(self) -> "IngestPolicyDocumentCommand":
        if self.tenant_id.int == 0:
            raise ValueError("The quarantine tenant cannot own knowledge.")
        if (
            self.effective_to is not None
            and self.effective_to <= self.effective_from
        ):
            raise ValueError("effective_to must be after effective_from.")
        if not self.allowed_roles:
            raise ValueError("Knowledge must allow at least one role.")
        if len(set(self.allowed_roles)) != len(self.allowed_roles):
            raise ValueError("Knowledge allowed_roles must be unique.")
        return self


class KnowledgeDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    tenant_id: UUID
    source_key: str
    document_version: str
    title: str
    source_uri: str
    media_type: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_content: str = Field(repr=False)
    effective_from: datetime
    effective_to: datetime | None
    allowed_roles: tuple[Role, ...]
    is_current: bool
    created_at: datetime


class KnowledgeChunk(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    tenant_id: UUID
    document_id: UUID
    chunk_index: int = Field(ge=0)
    section_path: tuple[str, ...]
    content: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_uri: str
    document_version: str
    source_line_start: int = Field(ge=1)
    source_line_end: int = Field(ge=1)
    effective_from: datetime
    effective_to: datetime | None
    allowed_roles: tuple[Role, ...]
    created_at: datetime

    @model_validator(mode="after")
    def line_range_must_be_ordered(self) -> "KnowledgeChunk":
        if self.source_line_end < self.source_line_start:
            raise ValueError("Chunk source line range is reversed.")
        return self


class KnowledgeIngestionRun(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    tenant_id: UUID
    idempotency_key: str
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_key: str
    document_version: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: KnowledgeIngestionStatus
    attempts: int = Field(ge=1)
    max_attempts: int = Field(ge=1)
    document_id: UUID | None
    chunk_count: int = Field(ge=0)
    error_code: str | None
    request_id: str
    trace_id: str
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class KnowledgeIngestionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run: KnowledgeIngestionRun
    document: KnowledgeDocument | None
    chunks: tuple[KnowledgeChunk, ...]
    deduplicated: bool
