from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
import json
from time import perf_counter, sleep
from typing import TypeVar
from uuid import uuid4

from pydantic import BaseModel, ValidationError
from opentelemetry.trace import SpanKind

from app.domain.model_call import ModelCallRecord, ModelCallStatus
from app.llm.contracts import (
    ProviderModelRequest,
    RawProviderResponse,
    StructuredLLMProvider,
    StructuredModelRequest,
    StructuredModelResponse,
)
from app.llm.errors import (
    ModelCallRecordingError,
    ModelGatewayError,
    ModelOutputValidationError,
    ModelProviderError,
    ModelProviderUnavailableError,
    ModelRequestRejectedError,
)
from app.repositories.model_call_repository import ModelCallRepository
from app.observability.tracing import (
    FailureDomain,
    add_safe_event,
    mark_span_error,
    operation_span,
    set_safe_attributes,
)
from app.observability.metrics import get_metrics

OutputT = TypeVar("OutputT", bound=BaseModel)


class ModelGateway:
    def __init__(
        self,
        provider: StructuredLLMProvider,
        call_repository: ModelCallRepository,
        *,
        model: str,
        reasoning_effort: str,
        max_output_tokens: int,
        max_attempts: int,
        retry_base_seconds: float,
        sleeper: Callable[[float], None] = sleep,
        wall_clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] = perf_counter,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive.")
        if retry_base_seconds <= 0:
            raise ValueError("retry_base_seconds must be positive.")
        self._provider = provider
        self._call_repository = call_repository
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._max_output_tokens = max_output_tokens
        self._max_attempts = max_attempts
        self._retry_base_seconds = retry_base_seconds
        self._sleeper = sleeper
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))
        self._monotonic_clock = monotonic_clock

    @property
    def model(self) -> str:
        return self._model

    @property
    def max_output_tokens(self) -> int:
        return self._max_output_tokens

    def generate(
        self,
        request: StructuredModelRequest[OutputT],
    ) -> StructuredModelResponse[OutputT]:
        metrics_started = self._monotonic_clock()
        with operation_span(
            f"chat {self._model}",
            kind=SpanKind.CLIENT,
            failure_domain=FailureDomain.MODEL,
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": self._provider.name,
                "gen_ai.request.model": self._model,
                "gen_ai.request.max_tokens": self._max_output_tokens,
                "resolveflow.component": "llm",
                "resolveflow.model.operation": request.operation,
                "resolveflow.prompt.name": request.prompt_name,
                "resolveflow.prompt.version": request.prompt_version,
                "resolveflow.prompt.hash": request.prompt_hash,
                "resolveflow.response_schema": request.response_model.__name__,
                "resolveflow.request_id": request.request_id,
                "resolveflow.trace_id": request.trace_id,
                "resolveflow.context.build_id": request.context_build_id,
            },
        ) as span:
            try:
                response = self._generate(request)
            except ModelCallRecordingError as exc:
                get_metrics().record_llm_call(
                    provider=self._provider.name,
                    model=self._model,
                    operation=request.operation,
                    status="failed",
                    error_code=exc.error_code,
                    duration=self._monotonic_clock() - metrics_started,
                )
                mark_span_error(
                    span,
                    FailureDomain.DATABASE,
                    exc.error_code,
                    retryable=False,
                )
                raise
            except ModelGatewayError as exc:
                get_metrics().record_llm_call(
                    provider=self._provider.name,
                    model=self._model,
                    operation=request.operation,
                    status="failed",
                    error_code=exc.error_code,
                    duration=self._monotonic_clock() - metrics_started,
                )
                mark_span_error(
                    span,
                    FailureDomain.MODEL,
                    exc.error_code,
                    retryable=isinstance(
                        exc,
                        ModelProviderUnavailableError,
                    ),
                )
                raise
            set_safe_attributes(
                span,
                {
                    "gen_ai.response.model": response.model,
                    "gen_ai.usage.input_tokens": response.input_tokens,
                    "gen_ai.usage.output_tokens": response.output_tokens,
                    "resolveflow.model.call_id": response.call_id,
                    "resolveflow.model.attempts": response.attempts,
                    "resolveflow.model.latency_ms": response.latency_ms,
                },
            )
            get_metrics().record_llm_call(
                provider=response.provider,
                model=response.model,
                operation=request.operation,
                status="succeeded",
                error_code=None,
                duration=response.latency_ms / 1000,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
            )
            if response.attempts > 1:
                add_safe_event(
                    span,
                    "model.retry_completed",
                    {"attempts": response.attempts},
                )
            return response

    def _generate(
        self,
        request: StructuredModelRequest[OutputT],
    ) -> StructuredModelResponse[OutputT]:
        call_id = uuid4()
        started_at = self._wall_clock()
        started_tick = self._monotonic_clock()
        attempt_error_codes: list[str] = []
        raw_response: RawProviderResponse | None = None

        for attempt in range(1, self._max_attempts + 1):
            try:
                raw_response = self._provider.complete(
                    ProviderModelRequest(
                        model=self._model,
                        reasoning_effort=self._reasoning_effort,
                        max_output_tokens=self._max_output_tokens,
                        instructions=request.instructions,
                        input_text=request.input_text,
                        response_model=request.response_model,
                        prompt_name=request.prompt_name,
                        prompt_version=request.prompt_version,
                        request_id=request.request_id,
                        trace_id=request.trace_id,
                    )
                )
            except ModelProviderError as exc:
                attempt_error_codes.append(exc.error_code)
                if exc.retryable and attempt < self._max_attempts:
                    self._sleeper(
                        self._retry_base_seconds * (2 ** (attempt - 1))
                    )
                    continue
                gateway_error: ModelGatewayError
                if exc.retryable:
                    gateway_error = ModelProviderUnavailableError(
                        exc.error_code
                    )
                else:
                    gateway_error = ModelRequestRejectedError(
                        exc.error_code
                    )
                self._record_failure(
                    request,
                    call_id=call_id,
                    attempts=attempt,
                    attempt_error_codes=attempt_error_codes,
                    error_code=gateway_error.error_code,
                    started_at=started_at,
                    started_tick=started_tick,
                )
                raise gateway_error from exc
            except Exception as exc:
                error = ModelRequestRejectedError(
                    "provider_unclassified_error"
                )
                attempt_error_codes.append(error.error_code)
                self._record_failure(
                    request,
                    call_id=call_id,
                    attempts=attempt,
                    attempt_error_codes=attempt_error_codes,
                    error_code=error.error_code,
                    started_at=started_at,
                    started_tick=started_tick,
                )
                raise error from exc

            try:
                output = _validate_output(
                    request.response_model,
                    raw_response.output,
                )
            except (ValidationError, TypeError, ValueError) as exc:
                error = ModelOutputValidationError()
                attempt_error_codes.append(error.error_code)
                self._record_failure(
                    request,
                    call_id=call_id,
                    attempts=attempt,
                    attempt_error_codes=attempt_error_codes,
                    error_code=error.error_code,
                    started_at=started_at,
                    started_tick=started_tick,
                    raw_response=raw_response,
                )
                raise error from exc

            completed_at = self._wall_clock()
            latency_ms = (
                self._monotonic_clock() - started_tick
            ) * 1000
            self._record(
                ModelCallRecord(
                    id=call_id,
                    tenant_id=request.tenant_id,
                    operation=request.operation,
                    provider=self._provider.name,
                    model=raw_response.model,
                    prompt_name=request.prompt_name,
                    prompt_version=request.prompt_version,
                    prompt_hash=request.prompt_hash,
                    response_schema_name=request.response_model.__name__,
                    response_schema_hash=_schema_hash(
                        request.response_model
                    ),
                    resource_type=request.resource_type,
                    resource_id=request.resource_id,
                    status=ModelCallStatus.SUCCEEDED,
                    attempts=attempt,
                    attempt_error_codes=tuple(attempt_error_codes),
                    latency_ms=latency_ms,
                    input_tokens=raw_response.input_tokens,
                    output_tokens=raw_response.output_tokens,
                    provider_request_id=raw_response.provider_request_id,
                    provider_response_id=raw_response.provider_response_id,
                    error_code=None,
                    request_id=request.request_id,
                    trace_id=request.trace_id,
                    started_at=started_at,
                    completed_at=completed_at,
                    context_build_id=request.context_build_id,
                )
            )
            return StructuredModelResponse(
                output=output,
                provider=self._provider.name,
                model=raw_response.model,
                prompt_name=request.prompt_name,
                prompt_version=request.prompt_version,
                call_id=call_id,
                attempts=attempt,
                latency_ms=latency_ms,
                input_tokens=raw_response.input_tokens,
                output_tokens=raw_response.output_tokens,
                provider_request_id=raw_response.provider_request_id,
                provider_response_id=raw_response.provider_response_id,
            )

        raise AssertionError("Model gateway retry loop exited unexpectedly.")

    def _record_failure(
        self,
        request: StructuredModelRequest,
        *,
        call_id,
        attempts: int,
        attempt_error_codes: list[str],
        error_code: str,
        started_at: datetime,
        started_tick: float,
        raw_response: RawProviderResponse | None = None,
    ) -> None:
        self._record(
            ModelCallRecord(
                id=call_id,
                tenant_id=request.tenant_id,
                operation=request.operation,
                provider=self._provider.name,
                model=(
                    raw_response.model
                    if raw_response is not None
                    else self._model
                ),
                prompt_name=request.prompt_name,
                prompt_version=request.prompt_version,
                prompt_hash=request.prompt_hash,
                response_schema_name=request.response_model.__name__,
                response_schema_hash=_schema_hash(
                    request.response_model
                ),
                resource_type=request.resource_type,
                resource_id=request.resource_id,
                status=ModelCallStatus.FAILED,
                attempts=attempts,
                attempt_error_codes=tuple(attempt_error_codes),
                latency_ms=(
                    self._monotonic_clock() - started_tick
                )
                * 1000,
                input_tokens=(
                    raw_response.input_tokens
                    if raw_response is not None
                    else None
                ),
                output_tokens=(
                    raw_response.output_tokens
                    if raw_response is not None
                    else None
                ),
                provider_request_id=(
                    raw_response.provider_request_id
                    if raw_response is not None
                    else None
                ),
                provider_response_id=(
                    raw_response.provider_response_id
                    if raw_response is not None
                    else None
                ),
                error_code=error_code,
                request_id=request.request_id,
                trace_id=request.trace_id,
                started_at=started_at,
                completed_at=self._wall_clock(),
                context_build_id=request.context_build_id,
            )
        )

    def _record(self, record: ModelCallRecord) -> None:
        try:
            self._call_repository.add(record)
        except Exception as exc:
            raise ModelCallRecordingError() from exc


def _validate_output(
    output_model: type[OutputT],
    output,
) -> OutputT:
    if isinstance(output, str):
        return output_model.model_validate_json(output)
    if isinstance(output, BaseModel):
        return output_model.model_validate(output.model_dump())
    return output_model.model_validate(output)


def _schema_hash(output_model: type[BaseModel]) -> str:
    serialized = json.dumps(
        output_model.model_json_schema(),
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(serialized).hexdigest()
