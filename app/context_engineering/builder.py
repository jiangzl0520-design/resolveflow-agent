from datetime import UTC, datetime
from hashlib import sha256
import json
from typing import Callable
from uuid import uuid4

from app.context_engineering.errors import (
    ContextEngineeringError,
    ContextTraceRecordingError,
    RequiredContextSecurityError,
    RequiredContextOverflowError,
)
from app.context_engineering.security import ContextSecurityPolicy
from app.context_engineering.tokens import TokenCounter
from app.domain.context import (
    ContextBuildRequest,
    ContextBuildResult,
    ContextBuildRun,
    ContextBuildStatus,
    ContextDecisionReason,
    ContextFragmentCandidate,
    ContextFragmentTrace,
    ContextSource,
    ContextTrustLevel,
    SelectedContextFragment,
)
from app.repositories.context_trace_repository import ContextTraceRepository
from app.observability.tracing import (
    FailureDomain,
    add_safe_event,
    mark_span_error,
    operation_span,
    set_safe_attributes,
)

_RUNTIME_PREAMBLE = "Choose the next action from this runtime snapshot."
_TRUST_RANK = {
    ContextTrustLevel.SYSTEM: 4,
    ContextTrustLevel.VERIFIED_INTERNAL: 3,
    ContextTrustLevel.USER_PROVIDED: 2,
    ContextTrustLevel.EXTERNAL_DATA: 1,
}


class ContextBuilder:
    def __init__(
        self,
        token_counter: TokenCounter,
        repository: ContextTraceRepository,
        *,
        minimum_relevance: int = 25,
        security_policy: ContextSecurityPolicy | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not 0 <= minimum_relevance <= 100:
            raise ValueError("minimum_relevance must be between 0 and 100.")
        self._token_counter = token_counter
        self._repository = repository
        self._minimum_relevance = minimum_relevance
        self._security_policy = security_policy or ContextSecurityPolicy()
        self._clock = clock

    def build(self, request: ContextBuildRequest) -> ContextBuildResult:
        with operation_span(
            "build agent context",
            failure_domain=FailureDomain.CONTEXT,
            attributes={
                "resolveflow.component": "context_builder",
                "resolveflow.context.model": request.model,
                "resolveflow.context.candidate_count": len(
                    request.candidates
                ),
                "resolveflow.context.budget_tokens": (
                    request.budget.input_budget_tokens
                ),
                "resolveflow.agent.run_id": request.agent_run_id,
                "resolveflow.agent.step_number": request.step_number,
                "resolveflow.request_id": request.request_id,
                "resolveflow.trace_id": request.trace_id,
            },
        ) as span:
            try:
                result = self._build(request)
            except ContextTraceRecordingError as exc:
                mark_span_error(
                    span,
                    FailureDomain.DATABASE,
                    exc.code,
                    retryable=exc.retryable,
                )
                raise
            except RequiredContextSecurityError as exc:
                mark_span_error(
                    span,
                    FailureDomain.SECURITY,
                    exc.code,
                    retryable=exc.retryable,
                )
                raise
            except ContextEngineeringError as exc:
                mark_span_error(
                    span,
                    FailureDomain.CONTEXT,
                    exc.code,
                    retryable=exc.retryable,
                )
                raise
            set_safe_attributes(
                span,
                {
                    "resolveflow.context.build_id": result.run.id,
                    "resolveflow.context.status": result.run.status.value,
                    "resolveflow.context.input_tokens": (
                        result.run.actual_input_tokens
                    ),
                    "resolveflow.context.included_count": (
                        result.run.included_fragment_count
                    ),
                    "resolveflow.context.dropped_count": (
                        result.run.dropped_fragment_count
                    ),
                },
            )
            for item in result.traces:
                add_safe_event(
                    span,
                    "context.fragment_decision",
                    {
                        "fragment_id": item.fragment_id,
                        "source": item.source.value,
                        "trust_level": item.trust_level.value,
                        "included": item.included,
                        "decision_reason": item.decision_reason.value,
                        "estimated_tokens": item.estimated_tokens,
                    },
                )
            return result

    def _build(self, request: ContextBuildRequest) -> ContextBuildResult:
        build_id = uuid4()
        decisions: dict[str, ContextDecisionReason] = {}
        candidates = [
            self._security_policy.protect(item)
            for item in request.candidates
        ]
        token_counts = {
            item.fragment_id: self._token_counter.count(
                _serialize(_fragment_document(item))
            )
            for item in candidates
        }

        selectable: list[ContextFragmentCandidate] = []
        for candidate in candidates:
            if not candidate.eligible:
                assert candidate.exclusion_reason is not None
                decisions[candidate.fragment_id] = candidate.exclusion_reason
            else:
                selectable.append(candidate)

        if any(
            candidate.required
            and decisions.get(candidate.fragment_id)
            is ContextDecisionReason.SECURITY_POLICY_BLOCKED
            for candidate in candidates
        ):
            for candidate in candidates:
                decisions.setdefault(
                    candidate.fragment_id,
                    ContextDecisionReason.BUILD_ABORTED,
                )
            run, traces = self._trace(
                request,
                candidates=candidates,
                build_id=build_id,
                decisions=decisions,
                token_counts=token_counts,
                selected=[],
                actual_input_tokens=0,
                status=ContextBuildStatus.FAILED,
                error_code="required_context_security_blocked",
            )
            self._record(run, traces)
            raise RequiredContextSecurityError()

        selectable = self._remove_superseded(selectable, decisions)
        selectable = self._remove_duplicates(selectable, decisions)
        filtered: list[ContextFragmentCandidate] = []
        for candidate in selectable:
            if (
                not candidate.required
                and candidate.relevance < self._minimum_relevance
            ):
                decisions[candidate.fragment_id] = (
                    ContextDecisionReason.LOW_RELEVANCE
                )
            else:
                filtered.append(candidate)

        required = sorted(
            (item for item in filtered if item.required),
            key=_selection_key,
        )
        optional = sorted(
            (item for item in filtered if not item.required),
            key=_selection_key,
        )
        selected = list(required)
        for item in required:
            decisions[item.fragment_id] = ContextDecisionReason.REQUIRED

        required_tokens = self._measure(request, selected)
        if required_tokens > request.budget.input_budget_tokens:
            for item in optional:
                decisions[item.fragment_id] = (
                    ContextDecisionReason.TOKEN_BUDGET_EXCEEDED
                )
            run, traces = self._trace(
                request,
                candidates=candidates,
                build_id=build_id,
                decisions=decisions,
                token_counts=token_counts,
                selected=selected,
                actual_input_tokens=required_tokens,
                status=ContextBuildStatus.FAILED,
                error_code="required_context_overflow",
            )
            self._record(run, traces)
            raise RequiredContextOverflowError()

        for candidate in optional:
            trial = [*selected, candidate]
            if self._measure(request, trial) <= request.budget.input_budget_tokens:
                selected.append(candidate)
                decisions[candidate.fragment_id] = (
                    ContextDecisionReason.SELECTED
                )
            else:
                decisions[candidate.fragment_id] = (
                    ContextDecisionReason.TOKEN_BUDGET_EXCEEDED
                )

        instructions, input_text = self._render(selected)
        actual_tokens = self._measure(request, selected)
        run, traces = self._trace(
            request,
            candidates=candidates,
            build_id=build_id,
            decisions=decisions,
            token_counts=token_counts,
            selected=selected,
            actual_input_tokens=actual_tokens,
            status=ContextBuildStatus.SUCCEEDED,
            error_code=None,
        )
        self._record(run, traces)
        selected_by_id = {item.fragment_id: item for item in selected}
        return ContextBuildResult(
            run=run,
            instructions=instructions,
            input_text=input_text,
            included_fragments=tuple(
                SelectedContextFragment(
                    fragment_id=item.fragment_id,
                    semantic_key=item.semantic_key,
                    source=item.source,
                    trust_level=item.trust_level,
                    content=item.content,
                    priority=item.priority,
                    relevance=item.relevance,
                    ordinal=item.ordinal,
                    token_count=token_counts[item.fragment_id],
                )
                for item in selected
                if item.fragment_id in selected_by_id
            ),
            traces=traces,
        )

    @staticmethod
    def _remove_superseded(
        candidates: list[ContextFragmentCandidate],
        decisions: dict[str, ContextDecisionReason],
    ) -> list[ContextFragmentCandidate]:
        winners: dict[str, ContextFragmentCandidate] = {}
        for candidate in candidates:
            if not candidate.replaceable:
                continue
            current = winners.get(candidate.semantic_key)
            if current is None or _freshness_key(candidate) > _freshness_key(
                current
            ):
                winners[candidate.semantic_key] = candidate
        result: list[ContextFragmentCandidate] = []
        for candidate in candidates:
            if (
                candidate.replaceable
                and winners[candidate.semantic_key].fragment_id
                != candidate.fragment_id
            ):
                decisions[candidate.fragment_id] = (
                    ContextDecisionReason.SUPERSEDED
                )
            else:
                result.append(candidate)
        return result

    @staticmethod
    def _remove_duplicates(
        candidates: list[ContextFragmentCandidate],
        decisions: dict[str, ContextDecisionReason],
    ) -> list[ContextFragmentCandidate]:
        result: list[ContextFragmentCandidate] = []
        seen: set[str] = set()
        for candidate in sorted(candidates, key=_selection_key):
            digest = _content_hash(candidate.content)
            if digest in seen and not candidate.required:
                decisions[candidate.fragment_id] = ContextDecisionReason.DUPLICATE
                continue
            seen.add(digest)
            result.append(candidate)
        return result

    def _measure(
        self,
        request: ContextBuildRequest,
        selected: list[ContextFragmentCandidate],
    ) -> int:
        instructions, input_text = self._render(selected)
        schema_text = (
            _serialize(request.response_schema)
            if request.response_schema is not None
            else ""
        )
        return (
            self._token_counter.count(instructions)
            + self._token_counter.count(input_text)
            + self._token_counter.count(schema_text)
        )

    @staticmethod
    def _render(
        selected: list[ContextFragmentCandidate],
    ) -> tuple[str, str]:
        system_items = [
            item for item in selected if item.source is ContextSource.SYSTEM_POLICY
        ]
        runtime_items = [
            item for item in selected if item.source is not ContextSource.SYSTEM_POLICY
        ]
        instructions = "\n\n".join(
            item.content
            if isinstance(item.content, str)
            else _serialize(item.content)
            for item in system_items
        )
        runtime_document = {
            "context_fragments": [
                _fragment_document(item) for item in runtime_items
            ]
        }
        input_text = (
            f"{_RUNTIME_PREAMBLE}\n"
            "<agent_runtime_data>"
            f"{_serialize(runtime_document)}"
            "</agent_runtime_data>"
        )
        return instructions, input_text

    def _trace(
        self,
        request: ContextBuildRequest,
        *,
        candidates: list[ContextFragmentCandidate],
        build_id,
        decisions: dict[str, ContextDecisionReason],
        token_counts: dict[str, int],
        selected: list[ContextFragmentCandidate],
        actual_input_tokens: int,
        status: ContextBuildStatus,
        error_code: str | None,
    ) -> tuple[ContextBuildRun, tuple[ContextFragmentTrace, ...]]:
        selected_ids = {item.fragment_id for item in selected}
        traces = tuple(
            ContextFragmentTrace(
                build_run_id=build_id,
                fragment_id=item.fragment_id,
                semantic_key=item.semantic_key,
                source=item.source,
                trust_level=item.trust_level,
                content_hash=_content_hash(item.content),
                priority=item.priority,
                relevance=item.relevance,
                ordinal=item.ordinal,
                estimated_tokens=token_counts[item.fragment_id],
                included=item.fragment_id in selected_ids,
                decision_reason=decisions[item.fragment_id],
            )
            for item in candidates
        )
        run = ContextBuildRun(
            id=build_id,
            tenant_id=request.tenant_id,
            agent_run_id=request.agent_run_id,
            step_number=request.step_number,
            model=request.model,
            status=status,
            context_window_tokens=request.budget.context_window_tokens,
            reserved_output_tokens=request.budget.reserved_output_tokens,
            reserved_reasoning_tokens=request.budget.reserved_reasoning_tokens,
            safety_margin_tokens=request.budget.safety_margin_tokens,
            input_budget_tokens=request.budget.input_budget_tokens,
            actual_input_tokens=actual_input_tokens,
            included_fragment_count=len(selected),
            dropped_fragment_count=len(candidates) - len(selected),
            error_code=error_code,
            request_id=request.request_id,
            trace_id=request.trace_id,
            created_at=self._clock(),
        )
        return run, traces

    def _record(
        self,
        run: ContextBuildRun,
        traces: tuple[ContextFragmentTrace, ...],
    ) -> None:
        try:
            self._repository.record(run, traces)
        except Exception as exc:
            raise ContextTraceRecordingError() from exc


def _fragment_document(candidate: ContextFragmentCandidate) -> dict[str, object]:
    return {
        "fragment_id": candidate.fragment_id,
        "semantic_key": candidate.semantic_key,
        "source": candidate.source.value,
        "trust_level": candidate.trust_level.value,
        "content": candidate.content,
    }


def _serialize(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _content_hash(value) -> str:
    return sha256(_serialize(value).encode("utf-8")).hexdigest()


def _selection_key(candidate: ContextFragmentCandidate) -> tuple[object, ...]:
    return (
        -int(candidate.required),
        -candidate.priority,
        -_TRUST_RANK[candidate.trust_level],
        -candidate.relevance,
        -candidate.ordinal,
        candidate.fragment_id,
    )


def _freshness_key(candidate: ContextFragmentCandidate) -> tuple[int, int, int]:
    return candidate.ordinal, candidate.priority, candidate.relevance
