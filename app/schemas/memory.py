from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.memory import (
    MemoryCategory,
    MemoryDecision,
    MemorySourceType,
)
from app.schemas.common import ResponseMeta


class MemoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

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


class MemoryDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=300)


class MemoryOperationRead(BaseModel):
    decision: MemoryDecision
    memory_id: UUID | None
    memory_key: str


class MemoryRead(BaseModel):
    id: UUID
    memory_key: str
    category: MemoryCategory
    value: str
    source_type: MemorySourceType
    confidence: float
    observed_at: datetime
    expires_at: datetime
    version: int


class MemoryOperationResponse(BaseModel):
    data: MemoryOperationRead
    meta: ResponseMeta


class MemoryListResponse(BaseModel):
    data: list[MemoryRead]
    meta: ResponseMeta
