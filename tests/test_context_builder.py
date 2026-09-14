from uuid import UUID

import pytest

from app.context_engineering.builder import ContextBuilder
from app.context_engineering.errors import (
    ContextTraceRecordingError,
    RequiredContextOverflowError,
)
from app.domain.context import (
    ContextBuildRequest,
    ContextBuildStatus,
    ContextDecisionReason,
    ContextFragmentCandidate,
    ContextSource,
    ContextTrustLevel,
    ContextWindowBudget,
)
from app.repositories.context_trace_repository import (
    InMemoryContextTraceRepository,
)

TENANT_ID = UUID("91000000-0000-0000-0000-000000000001")
RUN_ID = UUID("91000000-0000-0000-0000-000000000002")


class CharacterCounter:
    def count(self, text: str) -> int:
        return len(text)


def _candidate(
    fragment_id: str,
    source: ContextSource,
    content,
    *,
    semantic_key: str | None = None,
    priority: int = 50,
    relevance: int = 50,
    ordinal: int = 0,
    required: bool = False,
    replaceable: bool = False,
    eligible: bool = True,
    exclusion_reason: ContextDecisionReason | None = None,
) -> ContextFragmentCandidate:
    trust = (
        ContextTrustLevel.SYSTEM
        if source is ContextSource.SYSTEM_POLICY
        else ContextTrustLevel.USER_PROVIDED
    )
    return ContextFragmentCandidate(
        fragment_id=fragment_id,
        semantic_key=semantic_key or fragment_id,
        source=source,
        trust_level=trust,
        content=content,
        priority=priority,
        relevance=relevance,
        ordinal=ordinal,
        required=required,
        replaceable=replaceable,
        eligible=eligible,
        exclusion_reason=exclusion_reason,
    )


def _request(
    candidates: tuple[ContextFragmentCandidate, ...],
    *,
    max_input_tokens: int,
) -> ContextBuildRequest:
    return ContextBuildRequest(
        tenant_id=TENANT_ID,
        agent_run_id=RUN_ID,
        step_number=3,
        model="test-model",
        budget=ContextWindowBudget(
            context_window_tokens=20_000,
            reserved_output_tokens=500,
            reserved_reasoning_tokens=500,
            safety_margin_tokens=500,
            max_input_tokens=max_input_tokens,
        ),
        candidates=candidates,
        request_id="request-day15",
        trace_id="trace-day15",
    )


def test_long_context_keeps_goal_latest_correction_and_key_evidence() -> None:
    repository = InMemoryContextTraceRepository()
    candidates = [
        _candidate(
            "system",
            ContextSource.SYSTEM_POLICY,
            "Never execute refunds without deterministic gates.",
            priority=100,
            relevance=100,
            required=True,
        ),
        _candidate(
            "goal",
            ContextSource.CURRENT_GOAL,
            {"order_id": "10087", "goal": "investigate non-delivery"},
            priority=100,
            relevance=100,
            required=True,
        ),
        _candidate(
            "correction-old",
            ContextSource.USER_CORRECTION,
            {"order_id": "10086"},
            semantic_key="current-order",
            priority=99,
            relevance=100,
            ordinal=1,
            replaceable=True,
        ),
        _candidate(
            "correction-latest",
            ContextSource.USER_CORRECTION,
            {"order_id": "10087"},
            semantic_key="current-order",
            priority=99,
            relevance=100,
            ordinal=2,
            replaceable=True,
        ),
        _candidate(
            "key-evidence",
            ContextSource.CONFIRMED_FACT,
            {"delivery_proof": "missing"},
            priority=98,
            relevance=100,
        ),
    ]
    candidates.extend(
        _candidate(
            f"history-{index}",
            ContextSource.HISTORY,
            f"unrelated conversation {index} " + ("x" * 600),
            priority=10,
            relevance=30,
            ordinal=index,
        )
        for index in range(10)
    )

    result = ContextBuilder(CharacterCounter(), repository).build(
        _request(tuple(candidates), max_input_tokens=1_600)
    )

    selected_ids = {item.fragment_id for item in result.included_fragments}
    reasons = {item.fragment_id: item.decision_reason for item in result.traces}
    assert {"system", "goal", "correction-latest", "key-evidence"} <= selected_ids
    assert "correction-old" not in selected_ids
    assert reasons["correction-old"] is ContextDecisionReason.SUPERSEDED
    assert any(
        reason is ContextDecisionReason.TOKEN_BUDGET_EXCEEDED
        for fragment_id, reason in reasons.items()
        if fragment_id.startswith("history-")
    )
    assert result.run.actual_input_tokens <= result.run.input_budget_tokens
    assert repository.runs == [result.run]
    assert all(len(trace.content_hash) == 64 for trace in result.traces)


def test_every_exclusion_path_has_an_explicit_reason() -> None:
    repository = InMemoryContextTraceRepository()
    result = ContextBuilder(CharacterCounter(), repository).build(
        _request(
            (
                _candidate(
                    "system",
                    ContextSource.SYSTEM_POLICY,
                    "policy",
                    required=True,
                ),
                _candidate(
                    "goal",
                    ContextSource.CURRENT_GOAL,
                    "goal",
                    required=True,
                ),
                _candidate(
                    "low",
                    ContextSource.HISTORY,
                    "noise",
                    relevance=1,
                ),
                _candidate(
                    "stale",
                    ContextSource.TOOL_OBSERVATION,
                    {"order_id": "10086"},
                    eligible=False,
                    exclusion_reason=ContextDecisionReason.INVALIDATED,
                ),
            ),
            max_input_tokens=2_000,
        )
    )

    reasons = {item.fragment_id: item.decision_reason for item in result.traces}
    assert reasons["low"] is ContextDecisionReason.LOW_RELEVANCE
    assert reasons["stale"] is ContextDecisionReason.INVALIDATED


def test_required_context_overflow_is_recorded_then_fails_closed() -> None:
    repository = InMemoryContextTraceRepository()
    request = _request(
        (
            _candidate(
                "system",
                ContextSource.SYSTEM_POLICY,
                "s" * 400,
                required=True,
            ),
            _candidate(
                "goal",
                ContextSource.CURRENT_GOAL,
                "g" * 400,
                required=True,
            ),
        ),
        max_input_tokens=200,
    )

    with pytest.raises(RequiredContextOverflowError):
        ContextBuilder(CharacterCounter(), repository).build(request)

    assert repository.runs[0].status is ContextBuildStatus.FAILED
    assert repository.runs[0].error_code == "required_context_overflow"


def test_trace_storage_failure_prevents_unobservable_model_input() -> None:
    class FailingRepository:
        def record(self, run, traces) -> None:
            raise RuntimeError("database unavailable")

    request = _request(
        (
            _candidate(
                "system",
                ContextSource.SYSTEM_POLICY,
                "policy",
                required=True,
            ),
            _candidate(
                "goal",
                ContextSource.CURRENT_GOAL,
                "goal",
                required=True,
            ),
        ),
        max_input_tokens=2_000,
    )

    with pytest.raises(ContextTraceRecordingError):
        ContextBuilder(CharacterCounter(), FailingRepository()).build(request)
