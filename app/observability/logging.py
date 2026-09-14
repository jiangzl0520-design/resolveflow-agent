from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
import json
import logging
from typing import Any, Iterator

from app.observability.tracing import current_trace_id


_request_id: ContextVar[str] = ContextVar("request_id", default="unknown")
_trace_id: ContextVar[str] = ContextVar("trace_id", default="unknown")
_BLOCKED_KEY_PARTS = (
    "password", "secret", "token", "authorization", "cookie", "content",
    "prompt", "message", "email", "phone", "address", "question", "input",
    "output",
)
_ALLOWED_FIELDS = frozenset({
    "method", "route", "status_code", "duration_ms", "component", "outcome",
    "reason", "error_code", "error_type", "retryable", "workflow", "tool",
    "version", "model", "operation", "attempts", "step_count",
})


@contextmanager
def log_context(*, request_id: str, trace_id: str) -> Iterator[None]:
    request_token = _request_id.set(request_id)
    trace_token = _trace_id.set(trace_id)
    try:
        yield
    finally:
        _request_id.reset(request_token)
        _trace_id.reset(trace_token)


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "resolveflow_fields", {})
        document = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
            "request_id": _request_id.get(),
            "trace_id": _trace_id.get() if _trace_id.get() != "unknown" else current_trace_id(fallback="unknown"),
            **_safe_fields(fields),
        }
        return json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    if any(getattr(handler, "_resolveflow_json", False) for handler in root.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    handler._resolveflow_json = True  # type: ignore[attr-defined]
    root.addHandler(handler)


def log_event(logger: logging.Logger, event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, event, extra={"resolveflow_fields": _safe_fields(fields)})


def _safe_fields(fields: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in fields.items():
        normalized = str(key).lower()
        if (
            any(part in normalized for part in _BLOCKED_KEY_PARTS)
            or normalized not in _ALLOWED_FIELDS
        ):
            safe[str(key)] = "[REDACTED]"
        elif value is None or isinstance(value, (str, int, float, bool)):
            safe[str(key)] = value
        else:
            safe[str(key)] = str(value)
    return safe
