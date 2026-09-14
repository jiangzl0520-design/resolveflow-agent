from collections.abc import Iterable

from app.agent.models import AgentRunState
from app.domain.memory import LongTermMemory, MemorySourceType
from app.domain.context import (
    ContextDecisionReason,
    ContextFragmentCandidate,
    ContextSource,
    ContextTrustLevel,
)
from app.tools.contracts import ToolDescriptor, ToolObservation


def agent_context_candidates(
    state: AgentRunState,
    allowed_tools: tuple[ToolDescriptor, ...],
    *,
    system_instructions: str,
    memories: tuple[LongTermMemory, ...] = (),
    memory_error_code: str | None = None,
) -> tuple[ContextFragmentCandidate, ...]:
    """Translate runtime state into tagged, independently auditable context."""
    candidates: list[ContextFragmentCandidate] = [
        ContextFragmentCandidate(
            fragment_id="system:agent-policy",
            semantic_key="system:agent-policy",
            source=ContextSource.SYSTEM_POLICY,
            trust_level=ContextTrustLevel.SYSTEM,
            content=system_instructions,
            priority=100,
            relevance=100,
            required=True,
        ),
        ContextFragmentCandidate(
            fragment_id="goal:current",
            semantic_key="goal:current",
            source=ContextSource.CURRENT_GOAL,
            trust_level=ContextTrustLevel.USER_PROVIDED,
            content={
                "requested_outcome": state.goal,
                "authoritative_order_id": state.order_id,
                "category": state.category.value,
            },
            priority=100,
            relevance=100,
            required=True,
        ),
        ContextFragmentCandidate(
            fragment_id="criteria:investigation",
            semantic_key="criteria:investigation",
            source=ContextSource.COMPLETION_CRITERIA,
            trust_level=ContextTrustLevel.SYSTEM,
            content={
                "finish_only_with_supported_outcome": True,
                "refund_requires_backend_policy_and_human_review": True,
            },
            priority=99,
            relevance=100,
            required=True,
        ),
        ContextFragmentCandidate(
            fragment_id=f"state:step:{state.step_count + 1}",
            semantic_key="state:current",
            source=ContextSource.AGENT_STATE,
            trust_level=ContextTrustLevel.VERIFIED_INTERNAL,
            content={
                "step_number": state.step_count + 1,
                "remaining_steps": max(0, state.max_steps - state.step_count),
                "remaining_agent_tokens": max(
                    0,
                    state.max_total_tokens
                    - state.model_input_tokens
                    - state.model_output_tokens,
                ),
            },
            priority=98,
            relevance=100,
            required=True,
        ),
    ]

    candidates.extend(_observation_candidates(state))
    candidates.extend(_verification_candidates(state.verification_failures))
    candidates.extend(_memory_candidates(memories))
    if memory_error_code is not None:
        candidates.append(
            ContextFragmentCandidate(
                fragment_id="memory:retrieval-status",
                semantic_key="memory:retrieval-status",
                source=ContextSource.MEMORY,
                trust_level=ContextTrustLevel.VERIFIED_INTERNAL,
                content={"error_code": memory_error_code},
                priority=0,
                relevance=0,
                eligible=False,
                exclusion_reason=(
                    ContextDecisionReason.DEPENDENCY_UNAVAILABLE
                ),
            )
        )
    relevant_tools = relevant_tool_names(state)
    candidates.extend(
        _tool_candidates(allowed_tools, relevant_tools=relevant_tools)
    )
    return tuple(candidates)


def _memory_candidates(
    memories: tuple[LongTermMemory, ...],
) -> Iterable[ContextFragmentCandidate]:
    for memory in memories:
        assert memory.value is not None
        trust = (
            ContextTrustLevel.USER_PROVIDED
            if memory.source_type is MemorySourceType.EXPLICIT_USER
            else ContextTrustLevel.VERIFIED_INTERNAL
        )
        yield ContextFragmentCandidate(
            fragment_id=f"memory:{memory.id}",
            semantic_key=f"memory:{memory.memory_key}",
            source=ContextSource.MEMORY,
            trust_level=trust,
            content={
                "memory_key": memory.memory_key,
                "value": memory.value,
                "source_type": memory.source_type.value,
                "confidence": memory.confidence,
                "expires_at": memory.expires_at.isoformat(),
            },
            priority=74,
            relevance=80,
            ordinal=memory.version,
            replaceable=True,
        )


def relevant_tool_names(state: AgentRunState) -> frozenset[str]:
    """Select only tools that can advance the current evidence gap."""
    latest = state.observations[-1] if state.observations else None
    if (
        latest is not None
        and not latest.succeeded
        and latest.error is not None
        and latest.error.retryable
    ):
        return frozenset({latest.tool_name})

    order = _latest_valid_success(state, "order_lookup")
    if order is None:
        return frozenset({"order_lookup"})
    order_output = order.output
    assert order_output is not None
    if not getattr(order_output, "paid", False) or getattr(
        order_output, "status", None
    ) in {"cancelled", "refunded"}:
        return frozenset()

    missing: set[str] = set()
    if _latest_valid_success(state, "logistics_lookup") is None:
        missing.add("logistics_lookup")
    if _latest_valid_success(state, "policy_lookup") is None:
        missing.add("policy_lookup")
    return frozenset(missing)


def _latest_valid_success(
    state: AgentRunState,
    tool_name: str,
) -> ToolObservation | None:
    for observation in reversed(state.observations):
        if observation.tool_name != tool_name or not observation.succeeded:
            continue
        if _references_different_order(observation, state.order_id):
            continue
        return observation
    return None


def _observation_candidates(
    state: AgentRunState,
) -> Iterable[ContextFragmentCandidate]:
    for ordinal, observation in enumerate(state.observations, start=1):
        invalidated = _references_different_order(
            observation,
            state.order_id,
        )
        if observation.succeeded:
            assert observation.output is not None
            content = {
                "tool_name": observation.tool_name,
                "tool_version": observation.tool_version,
                "status": observation.status.value,
                "output": observation.output.model_dump(mode="json"),
            }
        else:
            assert observation.error is not None
            content = {
                "tool_name": observation.tool_name,
                "tool_version": observation.tool_version,
                "status": observation.status.value,
                "error": {
                    "kind": observation.error.kind.value,
                    "code": observation.error.code,
                    "retryable": observation.error.retryable,
                },
            }
        yield ContextFragmentCandidate(
            fragment_id=f"observation:{observation.call_id}",
            semantic_key=f"observation:{observation.tool_name}",
            source=ContextSource.TOOL_OBSERVATION,
            trust_level=ContextTrustLevel.EXTERNAL_DATA,
            content=content,
            priority=86,
            relevance=100 if not invalidated else 0,
            ordinal=ordinal,
            replaceable=True,
            eligible=not invalidated,
            exclusion_reason=(
                ContextDecisionReason.INVALIDATED if invalidated else None
            ),
        )


def _verification_candidates(
    failures: tuple[str, ...],
) -> Iterable[ContextFragmentCandidate]:
    for ordinal, code in enumerate(failures, start=1):
        yield ContextFragmentCandidate(
            fragment_id=f"verification:{ordinal}",
            semantic_key=f"verification:{code}",
            source=ContextSource.VERIFICATION_FEEDBACK,
            trust_level=ContextTrustLevel.VERIFIED_INTERNAL,
            content={"failure_code": code},
            priority=90,
            relevance=90,
            ordinal=ordinal,
            replaceable=True,
        )


def _tool_candidates(
    tools: tuple[ToolDescriptor, ...],
    *,
    relevant_tools: frozenset[str],
) -> Iterable[ContextFragmentCandidate]:
    for tool in tools:
        relevant = tool.name in relevant_tools
        yield ContextFragmentCandidate(
            fragment_id=f"tool:{tool.name}:{tool.version}",
            semantic_key=f"tool:{tool.name}",
            source=ContextSource.TOOL_DESCRIPTOR,
            trust_level=ContextTrustLevel.VERIFIED_INTERNAL,
            content={
                "allowed_tools": [
                    {
                        "name": tool.name,
                        "version": tool.version,
                        "description": tool.description,
                        "input_schema": tool.input_schema,
                        "risk_level": tool.risk_level.value,
                        "side_effect": tool.side_effect.value,
                    }
                ],
            },
            priority=80,
            relevance=100 if relevant else 0,
            eligible=relevant,
            exclusion_reason=(
                None
                if relevant
                else ContextDecisionReason.NOT_RELEVANT_TO_CURRENT_STEP
            ),
        )


def _references_different_order(
    observation: ToolObservation,
    current_order_id: str,
) -> bool:
    if not observation.succeeded or observation.output is None:
        return False
    observed_order_id = getattr(observation.output, "order_id", None)
    return (
        observed_order_id is not None
        and observed_order_id != current_order_id
    )
