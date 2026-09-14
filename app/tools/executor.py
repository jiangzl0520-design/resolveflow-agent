from collections.abc import Callable
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
)
from hashlib import sha256
import json
from time import perf_counter
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ValidationError
from opentelemetry.trace import SpanKind
from opentelemetry import context as otel_context
from opentelemetry.context import Context

from app.observability.tracing import (
    FailureDomain,
    mark_span_error,
    operation_span,
    set_safe_attributes,
)
from app.observability.metrics import get_metrics

from app.tools.contracts import (
    ToolCallRequest,
    ToolCallStatus,
    ToolErrorDetail,
    ToolFailureKind,
    ToolObservation,
)
from app.tools.errors import (
    ToolCallRecordingError,
    ToolDependencyUnavailableError,
    ToolExecutionGrantError,
    ToolIdempotencyConflictError,
    ToolNotFoundError,
    ToolRemoteAuthorizationError,
    ToolRemoteContractError,
    ToolRemoteTimeoutError,
    ToolResourceNotFoundError,
)
from app.tools.registry import ToolRegistry


class ToolCallRecorder(Protocol):
    def add(self, observation: ToolObservation) -> None: ...


class InMemoryToolCallRecorder:
    def __init__(self) -> None:
        self.observations: list[ToolObservation] = []

    def add(self, observation: ToolObservation) -> None:
        self.observations.append(observation)


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        recorder: ToolCallRecorder,
        *,
        worker_pool: ThreadPoolExecutor | None = None,
        monotonic_clock: Callable[[], float] = perf_counter,
    ) -> None:
        self._registry = registry
        self._recorder = recorder
        self._worker_pool = worker_pool or ThreadPoolExecutor(
            max_workers=8,
            thread_name_prefix="resolveflow-tool",
        )
        self._owns_worker_pool = worker_pool is None
        self._monotonic_clock = monotonic_clock

    def close(self) -> None:
        if self._owns_worker_pool:
            self._worker_pool.shutdown(wait=True, cancel_futures=True)

    def execute(self, request: ToolCallRequest) -> ToolObservation:
        with operation_span(
            f"execute {request.tool_name}",
            kind=SpanKind.CLIENT,
            failure_domain=FailureDomain.TOOL,
            attributes={
                "resolveflow.component": "tool",
                "resolveflow.tool.name": request.tool_name,
                "resolveflow.tool.version": request.tool_version,
                "resolveflow.tool.arguments_hash": _document_hash(
                    request.arguments
                ),
                "resolveflow.request_id": request.context.request_id,
                "resolveflow.trace_id": request.context.trace_id,
                "resolveflow.agent.run_id": request.context.agent_run_id,
                "resolveflow.agent.step_id": request.context.agent_step_id,
            },
        ) as span:
            observation = self._execute(request)
            get_metrics().record_tool_call(
                tool=request.tool_name,
                version=request.tool_version,
                status=observation.status.value,
                error_kind=(
                    observation.error.kind.value
                    if observation.error is not None
                    else None
                ),
                error_code=(
                    observation.error.code
                    if observation.error is not None
                    else None
                ),
                duration=observation.duration_ms / 1000,
            )
            set_safe_attributes(
                span,
                {
                    "resolveflow.tool.call_id": observation.call_id,
                    "resolveflow.tool.status": observation.status.value,
                    "resolveflow.tool.risk_level": observation.risk_level,
                    "resolveflow.tool.side_effect": observation.side_effect,
                    "resolveflow.tool.duration_ms": observation.duration_ms,
                    "resolveflow.tool.input_schema_hash": (
                        observation.input_schema_hash
                    ),
                    "resolveflow.tool.output_schema_hash": (
                        observation.output_schema_hash
                    ),
                },
            )
            if observation.error is not None:
                mark_span_error(
                    span,
                    FailureDomain.TOOL,
                    observation.error.code,
                    retryable=observation.error.retryable,
                )
                set_safe_attributes(
                    span,
                    {
                        "resolveflow.tool.error_kind": (
                            observation.error.kind.value
                        )
                    },
                )
            return observation

    def _execute(self, request: ToolCallRequest) -> ToolObservation:
        started = self._monotonic_clock()
        arguments_hash = _document_hash(request.arguments)
        try:
            definition = self._registry.resolve(
                request.tool_name,
                request.tool_version,
            )
        except ToolNotFoundError:
            return self._record(
                request,
                started=started,
                arguments_hash=arguments_hash,
                error=ToolErrorDetail(
                    kind=ToolFailureKind.SELECTION,
                    code="tool_not_found",
                    retryable=False,
                    message="The selected tool name or version is unavailable.",
                ),
            )

        if not request.context.actor.has_permission(
            definition.required_permission
        ):
            return self._record(
                request,
                started=started,
                arguments_hash=arguments_hash,
                definition=definition,
                error=ToolErrorDetail(
                    kind=ToolFailureKind.AUTHORIZATION,
                    code="tool_permission_denied",
                    retryable=False,
                    message="The actor is not allowed to use this tool.",
                ),
            )

        try:
            arguments = definition.input_model.model_validate(
                request.arguments
            )
        except ValidationError as exc:
            safe_errors = tuple(
                {
                    "type": item["type"],
                    "loc": tuple(str(part) for part in item["loc"]),
                    "msg": item["msg"],
                }
                for item in exc.errors(include_url=False)
            )
            return self._record(
                request,
                started=started,
                arguments_hash=arguments_hash,
                definition=definition,
                error=ToolErrorDetail(
                    kind=ToolFailureKind.ARGUMENT,
                    code="tool_arguments_invalid",
                    retryable=False,
                    message="Tool arguments do not satisfy the contract.",
                    validation_errors=safe_errors,
                ),
            )

        future: Future = self._worker_pool.submit(
            _invoke_with_context,
            otel_context.get_current(),
            definition.handler,
            arguments,
            request.context,
        )
        try:
            raw_output = future.result(
                timeout=definition.timeout_seconds
            )
        except FutureTimeoutError:
            future.cancel()
            return self._record(
                request,
                started=started,
                arguments_hash=arguments_hash,
                definition=definition,
                error=ToolErrorDetail(
                    kind=ToolFailureKind.TIMEOUT,
                    code="tool_timeout",
                    retryable=True,
                    message="The tool exceeded its time limit.",
                ),
            )
        except ToolResourceNotFoundError:
            return self._record(
                request,
                started=started,
                arguments_hash=arguments_hash,
                definition=definition,
                error=ToolErrorDetail(
                    kind=ToolFailureKind.RESOURCE,
                    code="tool_resource_not_found",
                    retryable=False,
                    message="The requested resource was not found.",
                ),
            )
        except ToolRemoteAuthorizationError as exc:
            return self._record(
                request,
                started=started,
                arguments_hash=arguments_hash,
                definition=definition,
                error=ToolErrorDetail(
                    kind=ToolFailureKind.AUTHORIZATION,
                    code=exc.error_code,
                    retryable=False,
                    message=(
                        "The remote tool rejected the service identity."
                    ),
                ),
            )
        except ToolRemoteTimeoutError as exc:
            return self._record(
                request,
                started=started,
                arguments_hash=arguments_hash,
                definition=definition,
                error=ToolErrorDetail(
                    kind=ToolFailureKind.TIMEOUT,
                    code=exc.error_code,
                    retryable=True,
                    message="The remote tool exceeded its time limit.",
                ),
            )
        except ToolRemoteContractError as exc:
            return self._record(
                request,
                started=started,
                arguments_hash=arguments_hash,
                definition=definition,
                error=ToolErrorDetail(
                    kind=ToolFailureKind.OUTPUT_CONTRACT,
                    code=exc.error_code,
                    retryable=False,
                    message=(
                        "The remote tool response violated its contract."
                    ),
                ),
            )
        except ToolDependencyUnavailableError as exc:
            return self._record(
                request,
                started=started,
                arguments_hash=arguments_hash,
                definition=definition,
                error=ToolErrorDetail(
                    kind=ToolFailureKind.DEPENDENCY,
                    code=exc.error_code,
                    retryable=True,
                    message="The tool dependency is temporarily unavailable.",
                ),
            )
        except ToolIdempotencyConflictError:
            return self._record(
                request,
                started=started,
                arguments_hash=arguments_hash,
                definition=definition,
                error=ToolErrorDetail(
                    kind=ToolFailureKind.CONFLICT,
                    code="tool_idempotency_conflict",
                    retryable=False,
                    message=(
                        "The idempotency key was already used with "
                        "different arguments."
                    ),
                ),
            )
        except ToolExecutionGrantError:
            return self._record(
                request,
                started=started,
                arguments_hash=arguments_hash,
                definition=definition,
                error=ToolErrorDetail(
                    kind=ToolFailureKind.AUTHORIZATION,
                    code="tool_execution_grant_invalid",
                    retryable=False,
                    message=(
                        "The backend-issued execution grant is missing "
                        "or does not match the approved operation."
                    ),
                ),
            )
        except Exception:
            return self._record(
                request,
                started=started,
                arguments_hash=arguments_hash,
                definition=definition,
                error=ToolErrorDetail(
                    kind=ToolFailureKind.INTERNAL,
                    code="tool_execution_failed",
                    retryable=False,
                    message="The tool failed without a safe public detail.",
                ),
            )

        try:
            output = _validate_output(
                definition.output_model,
                raw_output,
            )
        except (ValidationError, TypeError, ValueError):
            return self._record(
                request,
                started=started,
                arguments_hash=arguments_hash,
                definition=definition,
                error=ToolErrorDetail(
                    kind=ToolFailureKind.OUTPUT_CONTRACT,
                    code="tool_output_invalid",
                    retryable=False,
                    message="Tool output violated its declared contract.",
                ),
            )

        return self._record(
            request,
            started=started,
            arguments_hash=arguments_hash,
            definition=definition,
            output=output,
        )

    def _record(
        self,
        request: ToolCallRequest,
        *,
        started: float,
        arguments_hash: str,
        definition=None,
        output: BaseModel | None = None,
        error: ToolErrorDetail | None = None,
    ) -> ToolObservation:
        observation = ToolObservation(
            call_id=uuid4(),
            tenant_id=request.context.tenant_id,
            actor_id=request.context.actor.actor_id,
            tool_name=request.tool_name,
            tool_version=request.tool_version,
            input_schema_hash=(
                _document_hash(definition.input_schema)
                if definition is not None
                else None
            ),
            output_schema_hash=(
                _document_hash(definition.output_schema)
                if definition is not None
                else None
            ),
            status=(
                ToolCallStatus.SUCCEEDED
                if error is None
                else ToolCallStatus.FAILED
            ),
            risk_level=(
                definition.risk_level
                if definition is not None
                else None
            ),
            side_effect=(
                definition.side_effect
                if definition is not None
                else None
            ),
            output=output,
            error=error,
            arguments_hash=arguments_hash,
            duration_ms=(
                self._monotonic_clock() - started
            )
            * 1000,
            request_id=request.context.request_id,
            trace_id=request.context.trace_id,
            agent_run_id=request.context.agent_run_id,
            agent_step_id=request.context.agent_step_id,
        )
        try:
            self._recorder.add(observation)
        except Exception as exc:
            raise ToolCallRecordingError(
                "Tool observation could not be recorded."
            ) from exc
        return observation


def _validate_output(
    output_model: type[BaseModel],
    output,
) -> BaseModel:
    if isinstance(output, BaseModel):
        return output_model.model_validate(output.model_dump())
    return output_model.model_validate(output)


def _invoke_with_context(
    parent_context: Context,
    handler,
    arguments,
    execution_context,
):
    token = otel_context.attach(parent_context)
    try:
        return handler(arguments, execution_context)
    finally:
        otel_context.detach(token)


def _document_hash(document: dict) -> str:
    serialized = json.dumps(
        document,
        default=str,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(serialized).hexdigest()
