from dataclasses import dataclass
from typing import Any, Iterable

from opentelemetry.trace import StatusCode

from app.observability.tracing import FailureDomain


@dataclass(frozen=True, slots=True)
class TraceFailureDiagnosis:
    trace_id: str
    domain: FailureDomain
    error_code: str
    span_name: str
    depth: int


class TraceFailureAnalyzer:
    """Locate the deepest explicitly classified failed span in one trace."""

    def diagnose(
        self,
        spans: Iterable[Any],
    ) -> TraceFailureDiagnosis | None:
        items = list(spans)
        if not items:
            return None
        by_id = {
            item.context.span_id: item
            for item in items
            if getattr(item, "context", None) is not None
        }
        candidates: list[tuple[int, Any, FailureDomain, str]] = []
        for span in items:
            attributes = dict(getattr(span, "attributes", {}) or {})
            raw_domain = attributes.get("resolveflow.failure.domain")
            status = getattr(getattr(span, "status", None), "status_code", None)
            if raw_domain is None and status is not StatusCode.ERROR:
                continue
            try:
                domain = FailureDomain(raw_domain or FailureDomain.UNKNOWN.value)
            except ValueError:
                domain = FailureDomain.UNKNOWN
            error_code = str(
                attributes.get("resolveflow.error.code")
                or attributes.get("error.type")
                or "unclassified_error"
            )
            candidates.append(
                (self._depth(span, by_id), span, domain, error_code)
            )
        if not candidates:
            return None
        depth, span, domain, error_code = max(
            candidates,
            key=lambda item: (
                item[0],
                item[2] not in {
                    FailureDomain.API,
                    FailureDomain.WORKER,
                    FailureDomain.AGENT,
                    FailureDomain.UNKNOWN,
                },
            ),
        )
        return TraceFailureDiagnosis(
            trace_id=format(span.context.trace_id, "032x"),
            domain=domain,
            error_code=error_code,
            span_name=span.name,
            depth=depth,
        )

    @staticmethod
    def _depth(span: Any, by_id: dict[int, Any]) -> int:
        depth = 0
        parent = getattr(span, "parent", None)
        seen: set[int] = set()
        while parent is not None and parent.span_id not in seen:
            seen.add(parent.span_id)
            depth += 1
            parent_span = by_id.get(parent.span_id)
            if parent_span is None:
                break
            parent = getattr(parent_span, "parent", None)
        return depth
