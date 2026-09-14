"""OpenTelemetry tracing and trace-based failure diagnosis."""

from app.observability.tracing import (
    FailureDomain,
    add_safe_event,
    configure_global_tracing,
    current_trace_id,
    extract_trace_context,
    identifier_hash,
    inject_trace_context,
    mark_span_error,
    operation_span,
    set_safe_attributes,
)

__all__ = [
    "FailureDomain",
    "add_safe_event",
    "configure_global_tracing",
    "current_trace_id",
    "extract_trace_context",
    "identifier_hash",
    "inject_trace_context",
    "mark_span_error",
    "operation_span",
    "set_safe_attributes",
]
