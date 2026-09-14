from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from re import fullmatch
from typing import Callable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MemoryCategory(StrEnum):
    USER_PREFERENCE = "user_preference"
    CUSTOMER_PROFILE = "customer_profile"


class MemorySourceType(StrEnum):
    EXPLICIT_USER = "explicit_user"
    VERIFIED_SYSTEM = "verified_system"
    HUMAN_REVIEWER = "human_reviewer"
    MODEL_INFERENCE = "model_inference"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    CONFLICTED = "conflicted"
    DELETED = "deleted"
    EXPIRED = "expired"


class MemoryDecision(StrEnum):
    CREATED = "created"
    UNCHANGED = "unchanged"
    UPDATED = "updated"
    CONFLICTED = "conflicted"
    CONFIRMATION_REQUIRED = "confirmation_required"
    REJECTED = "rejected"
    DELETED = "deleted"
    NOT_FOUND = "not_found"


class MemoryWriteCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subject_id: str = Field(min_length=1, max_length=128)
    memory_key: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    )
    value: str = Field(min_length=1, max_length=500)
    source_type: MemorySourceType
    source_reference: str = Field(min_length=1, max_length=256)
    confidence: float = Field(ge=0, le=1)
    observed_at: datetime
    expires_at: datetime
    idempotency_key: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=64)
    trace_id: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def expiry_follows_observation(self) -> "MemoryWriteCommand":
        if self.expires_at <= self.observed_at:
            raise ValueError("Memory expiry must follow observation time.")
        return self


class MemoryDeleteCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subject_id: str = Field(min_length=1, max_length=128)
    memory_key: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    )
    reason: str = Field(min_length=3, max_length=300)
    idempotency_key: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=64)
    trace_id: str = Field(min_length=1, max_length=64)


class LongTermMemory(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    tenant_id: UUID
    subject_id: str
    memory_key: str
    category: MemoryCategory
    value: str | None
    value_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: MemoryStatus
    version: int = Field(ge=1)
    source_type: MemorySourceType
    source_reference_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    confidence: float = Field(ge=0, le=1)
    observed_at: datetime
    expires_at: datetime
    created_by_actor_id: str
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def active_memory_has_value(self) -> "LongTermMemory":
        if self.status in {
            MemoryStatus.ACTIVE,
            MemoryStatus.CONFLICTED,
            MemoryStatus.SUPERSEDED,
        } and self.value is None:
            raise ValueError("Non-deleted memory must retain its value.")
        if self.status is MemoryStatus.DELETED and self.value is not None:
            raise ValueError("Deleted memory content must be redacted.")
        return self


class MemoryOperationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: MemoryDecision
    memory_id: UUID | None = None
    memory_key: str


class MemoryAuditEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    tenant_id: UUID
    subject_id: str
    memory_key: str
    memory_id: UUID | None
    actor_id: str
    decision: MemoryDecision
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str
    request_id: str
    trace_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MemoryMutation:
    updated: tuple[LongTermMemory, ...]
    created: LongTermMemory | None
    result: MemoryOperationResult


@dataclass(frozen=True, slots=True)
class MemoryTransaction:
    tenant_id: UUID
    subject_id: str
    memory_key: str
    actor_id: str
    request_hash: str
    candidate_hash: str
    idempotency_key: str
    request_id: str
    trace_id: str
    created_at: datetime

    def __post_init__(self) -> None:
        if not fullmatch(r"[0-9a-f]{64}", self.request_hash):
            raise ValueError("request_hash must be SHA-256 hex.")
        if not fullmatch(r"[0-9a-f]{64}", self.candidate_hash):
            raise ValueError("candidate_hash must be SHA-256 hex.")


MemoryMutationResolver = Callable[
    [tuple[LongTermMemory, ...]],
    MemoryMutation,
]
