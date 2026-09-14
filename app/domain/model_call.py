from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class ModelCallStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ModelCallRecord:
    id: UUID
    tenant_id: UUID
    operation: str
    provider: str
    model: str
    prompt_name: str
    prompt_version: str
    prompt_hash: str
    response_schema_name: str
    response_schema_hash: str
    resource_type: str
    resource_id: str
    status: ModelCallStatus
    attempts: int
    attempt_error_codes: tuple[str, ...]
    latency_ms: float
    input_tokens: int | None
    output_tokens: int | None
    provider_request_id: str | None
    provider_response_id: str | None
    error_code: str | None
    request_id: str
    trace_id: str
    started_at: datetime
    completed_at: datetime
    context_build_id: UUID | None = None
