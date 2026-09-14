from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar
from uuid import UUID

from pydantic import BaseModel

StructuredOutput = TypeVar("StructuredOutput", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class StructuredModelRequest(Generic[StructuredOutput]):
    tenant_id: UUID
    operation: str
    prompt_name: str
    prompt_version: str
    prompt_hash: str
    resource_type: str
    resource_id: str
    instructions: str
    input_text: str
    response_model: type[StructuredOutput]
    request_id: str
    trace_id: str
    context_build_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ProviderModelRequest(Generic[StructuredOutput]):
    model: str
    reasoning_effort: str
    max_output_tokens: int
    instructions: str
    input_text: str
    response_model: type[StructuredOutput]
    prompt_name: str
    prompt_version: str
    request_id: str
    trace_id: str


@dataclass(frozen=True, slots=True)
class RawProviderResponse:
    output: Any
    model: str
    provider_request_id: str | None = None
    provider_response_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class StructuredModelResponse(Generic[StructuredOutput]):
    output: StructuredOutput
    provider: str
    model: str
    prompt_name: str
    prompt_version: str
    call_id: UUID
    attempts: int
    latency_ms: float
    input_tokens: int | None
    output_tokens: int | None
    provider_request_id: str | None
    provider_response_id: str | None


class StructuredLLMProvider(Protocol):
    @property
    def name(self) -> str: ...

    def complete(
        self,
        request: ProviderModelRequest[StructuredOutput],
    ) -> RawProviderResponse: ...
