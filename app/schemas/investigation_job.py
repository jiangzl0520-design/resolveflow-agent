from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.domain.investigation_job import (
    InvestigationJobEventType,
    InvestigationJobStatus,
    InvestigationJobType,
)
from app.schemas.common import ResponseMeta


class InvestigationJobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ticket_id: UUID
    job_type: InvestigationJobType
    status: InvestigationJobStatus
    actor_id: str
    request_id: str
    trace_id: str
    attempts: int
    max_attempts: int
    version: int
    next_attempt_at: datetime | None
    cancel_requested_at: datetime | None
    dispatched_at: datetime | None
    dispatch_attempts: int
    last_dispatch_error_code: str | None
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class InvestigationJobEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    job_id: UUID
    event_type: InvestigationJobEventType
    from_status: InvestigationJobStatus | None
    to_status: InvestigationJobStatus
    attempt: int
    reason: str
    request_id: str
    trace_id: str
    payload: dict[str, Any]
    created_at: datetime


class InvestigationJobResponse(BaseModel):
    data: InvestigationJobRead
    meta: ResponseMeta


class InvestigationJobEventListResponse(BaseModel):
    data: list[InvestigationJobEventRead]
    meta: ResponseMeta
