from re import match
from time import perf_counter
from typing import Any

from opentelemetry import context as otel_context, trace
from opentelemetry.trace import SpanKind
from sqlalchemy import Engine, event

from app.observability.tracing import (
    FailureDomain,
    mark_span_error,
    set_safe_attributes,
)
from app.observability.metrics import get_metrics

_SPAN_KEY = "_resolveflow_otel_span"
_TOKEN_KEY = "_resolveflow_otel_token"
_START_KEY = "_resolveflow_metrics_started"
_OPERATION_KEY = "_resolveflow_db_operation"
_SYSTEM_KEY = "_resolveflow_db_system"


def instrument_sqlalchemy_engine(engine: Engine) -> None:
    """Create DB client spans without recording SQL text or parameters."""
    if getattr(engine, "_resolveflow_otel_instrumented", False):
        return
    setattr(engine, "_resolveflow_otel_instrumented", True)

    @event.listens_for(engine, "before_cursor_execute")
    def before_cursor_execute(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        execution_context,
        _executemany: bool,
    ) -> None:
        operation = _sql_operation(statement)
        tracer = trace.get_tracer("resolveflow.database", "0.1.0")
        span = tracer.start_span(
            f"DB {operation}",
            kind=SpanKind.CLIENT,
            record_exception=False,
            set_status_on_exception=False,
        )
        set_safe_attributes(
            span,
            {
                "db.system.name": engine.dialect.name,
                "db.operation.name": operation,
                "resolveflow.component": "database",
            },
        )
        token = otel_context.attach(trace.set_span_in_context(span))
        setattr(execution_context, _SPAN_KEY, span)
        setattr(execution_context, _TOKEN_KEY, token)
        setattr(execution_context, _START_KEY, perf_counter())
        setattr(execution_context, _OPERATION_KEY, operation)
        setattr(execution_context, _SYSTEM_KEY, engine.dialect.name)

    @event.listens_for(engine, "after_cursor_execute")
    def after_cursor_execute(
        _connection,
        _cursor,
        _statement,
        _parameters,
        execution_context,
        _executemany,
    ) -> None:
        _finish_execution_span(execution_context, status="succeeded")

    @event.listens_for(engine, "handle_error")
    def handle_error(exception_context) -> None:
        execution_context = exception_context.execution_context
        if execution_context is None:
            return
        span = getattr(execution_context, _SPAN_KEY, None)
        if span is not None:
            mark_span_error(
                span,
                FailureDomain.DATABASE,
                type(exception_context.original_exception).__name__,
            )
        _finish_execution_span(execution_context, status="failed")


def _finish_execution_span(execution_context: Any, *, status: str) -> None:
    span = getattr(execution_context, _SPAN_KEY, None)
    token = getattr(execution_context, _TOKEN_KEY, None)
    if token is not None:
        otel_context.detach(token)
        setattr(execution_context, _TOKEN_KEY, None)
    if span is not None:
        span.end()
        setattr(execution_context, _SPAN_KEY, None)
    started = getattr(execution_context, _START_KEY, None)
    if started is not None:
        get_metrics().record_database(
            system=getattr(execution_context, _SYSTEM_KEY, "unknown"),
            operation=getattr(execution_context, _OPERATION_KEY, "execute").lower(),
            status=status,
            duration=perf_counter() - started,
        )
        setattr(execution_context, _START_KEY, None)


def _sql_operation(statement: str) -> str:
    found = match(r"\s*([A-Za-z]+)", statement or "")
    if found is None:
        return "EXECUTE"
    operation = found.group(1).upper()
    if operation == "WITH":
        return "QUERY"
    return operation[:32]
