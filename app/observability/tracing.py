from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from enum import StrEnum
from hashlib import sha256
from threading import Lock
from typing import Any
from uuid import uuid4

from opentelemetry import propagate, trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from app.core.config import Settings, TraceExporter
from app.security.content import ContentSecurityFlag, UntrustedContentGuard

INSTRUMENTATION_NAME = "resolveflow.observability"
INSTRUMENTATION_VERSION = "0.1.0"
MAX_ATTRIBUTE_LENGTH = 512


class FailureDomain(StrEnum):
    API = "api"
    WORKER = "worker"
    AGENT = "agent"
    MODEL = "model"
    RETRIEVAL = "retrieval"
    TOOL = "tool"
    DATABASE = "database"
    POLICY = "policy"
    CONTEXT = "context"
    SECURITY = "security"
    BUSINESS = "business"
    UNKNOWN = "unknown"


_configuration_lock = Lock()
_configured_provider: TracerProvider | None = None
_configured_exporter_ids: set[int] = set()
_content_guard = UntrustedContentGuard()
_safe_token_attribute_keys = frozenset(
    {
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "resolveflow.agent.total_tokens",
        "resolveflow.context.input_tokens",
        "resolveflow.context.budget_tokens",
    }
)
_sensitive_key_parts = frozenset(
    {
        "authorization",
        "cookie",
        "password",
        "secret",
        "api_key",
        "access_token",
        "refresh_token",
        "input.messages",
        "output.messages",
        "prompt.content",
        "user.content",
    }
)


def configure_global_tracing(
    settings: Settings,
    *,
    exporter: SpanExporter | None = None,
) -> TracerProvider | None:
    """Install one process-wide SDK provider when exporting is enabled.

    Tests may pass an in-memory exporter. Application defaults stay no-op until
    an exporter is configured, so importing a module never opens a network
    connection or prints spans unexpectedly.
    """
    global _configured_provider
    if exporter is None and settings.trace_exporter is TraceExporter.NONE:
        return _configured_provider
    with _configuration_lock:
        current_provider = trace.get_tracer_provider()
        if (
            _configured_provider is None
            and isinstance(current_provider, TracerProvider)
        ):
            _configured_provider = current_provider
        if _configured_provider is not None:
            if (
                exporter is not None
                and id(exporter) not in _configured_exporter_ids
            ):
                _configured_provider.add_span_processor(
                    SimpleSpanProcessor(exporter)
                )
                _configured_exporter_ids.add(id(exporter))
            return _configured_provider
        provider = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": settings.trace_service_name,
                    "service.version": INSTRUMENTATION_VERSION,
                    "deployment.environment.name": settings.environment.value,
                }
            ),
            sampler=ParentBased(TraceIdRatioBased(settings.trace_sample_ratio)),
        )
        resolved_exporter = exporter or _build_exporter(settings)
        if exporter is not None:
            provider.add_span_processor(SimpleSpanProcessor(resolved_exporter))
            _configured_exporter_ids.add(id(resolved_exporter))
        else:
            provider.add_span_processor(BatchSpanProcessor(resolved_exporter))
            _configured_exporter_ids.add(id(resolved_exporter))
        trace.set_tracer_provider(provider)
        _configured_provider = provider
        return provider


def _build_exporter(settings: Settings) -> SpanExporter:
    if settings.trace_exporter is TraceExporter.CONSOLE:
        return ConsoleSpanExporter()
    if settings.trace_exporter is TraceExporter.OTLP:
        return OTLPSpanExporter(endpoint=settings.trace_otlp_endpoint)
    raise ValueError("A trace exporter must be configured.")


@contextmanager
def operation_span(
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: Mapping[str, Any] | None = None,
    parent_context: Context | None = None,
    failure_domain: FailureDomain = FailureDomain.UNKNOWN,
) -> Iterator[Span]:
    tracer = trace.get_tracer(
        INSTRUMENTATION_NAME,
        INSTRUMENTATION_VERSION,
    )
    with tracer.start_as_current_span(
        name,
        context=parent_context,
        kind=kind,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        set_safe_attributes(span, attributes or {})
        try:
            yield span
        except BaseException as exc:
            status = getattr(span, "status", None)
            if getattr(status, "status_code", None) is not StatusCode.ERROR:
                mark_span_error(
                    span,
                    failure_domain,
                    type(exc).__name__,
                )
            raise


def mark_span_error(
    span: Span,
    domain: FailureDomain,
    error_code: str,
    *,
    retryable: bool | None = None,
) -> None:
    safe_code = _sanitize_value(error_code)
    span.set_status(Status(StatusCode.ERROR, str(safe_code)))
    set_safe_attributes(
        span,
        {
            "error.type": safe_code,
            "resolveflow.failure.domain": domain.value,
            "resolveflow.error.code": safe_code,
            **(
                {"resolveflow.error.retryable": retryable}
                if retryable is not None
                else {}
            ),
        },
    )


def set_safe_attributes(
    span: Span,
    attributes: Mapping[str, Any],
) -> None:
    if not span.is_recording():
        return
    for key, value in attributes.items():
        normalized_key = str(key).strip()
        if not normalized_key or value is None:
            continue
        if _is_sensitive_key(normalized_key):
            span.set_attribute(normalized_key, "[REDACTED]")
            continue
        safe_value = _sanitize_value(value)
        if safe_value is not None:
            span.set_attribute(normalized_key, safe_value)


def add_safe_event(
    span: Span,
    name: str,
    attributes: Mapping[str, Any] | None = None,
) -> None:
    safe_attributes: dict[str, Any] = {}
    for key, value in (attributes or {}).items():
        if value is None:
            continue
        normalized_key = str(key).strip()
        if _is_sensitive_key(normalized_key):
            safe_attributes[normalized_key] = "[REDACTED]"
        else:
            safe_value = _sanitize_value(value)
            if safe_value is not None:
                safe_attributes[normalized_key] = safe_value
    span.add_event(str(_sanitize_value(name)), attributes=safe_attributes)


def inject_trace_context(
    carrier: MutableMapping[str, str] | None = None,
) -> MutableMapping[str, str]:
    resolved = carrier if carrier is not None else {}
    propagate.inject(resolved)
    return resolved


def extract_trace_context(carrier: Mapping[str, Any]) -> Context:
    normalized = {str(key): str(value) for key, value in carrier.items()}
    return propagate.extract(normalized)


def current_trace_id(*, fallback: str | None = None) -> str:
    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        return format(span_context.trace_id, "032x")
    return fallback or str(uuid4())


def identifier_hash(value: Any) -> str:
    return sha256(str(value).encode("utf-8")).hexdigest()


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in _safe_token_attribute_keys:
        return False
    return any(part in lowered for part in _sensitive_key_parts)


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, str):
        protected = _content_guard.protect(value)
        if ContentSecurityFlag.PROMPT_INJECTION in protected.flags:
            return "[UNTRUSTED_CONTENT]"
        return protected.value[:MAX_ATTRIBUTE_LENGTH]
    if isinstance(value, (tuple, list, set, frozenset)):
        sanitized = tuple(_sanitize_value(item) for item in value)
        return tuple(item for item in sanitized if item is not None)
    return _sanitize_value(str(value))
