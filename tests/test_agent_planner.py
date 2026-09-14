from dataclasses import replace

from app.agent.models import AgentRunStatus, InvestigationOutcome
from app.agent.planner import AGENT_PROMPT, ModelGatewayAgentPlanner
from app.agent.runner import AgentRunner
from app.context_engineering.agent_context import (
    agent_context_candidates,
    relevant_tool_names,
)
from app.domain.context import ContextDecisionReason, ContextSource
from app.llm.contracts import RawProviderResponse
from app.llm.fake_provider import FakeLLMProvider
from app.llm.gateway import ModelGateway
from app.repositories.model_call_repository import (
    InMemoryModelCallRepository,
)
from app.tools.executor import InMemoryToolCallRecorder, ToolExecutor
from app.tools.registry import ToolRegistry
from tests.test_agent_loop import command, source
from app.tools.after_sales import build_after_sales_tools


def _tool_output(name: str, arguments: dict) -> dict:
    return {
        "action": "call_tool",
        "reason": "收集完成当前调查所需的下一项可信证据",
        "tool_name": name,
        "tool_version": "1.0.0",
        "arguments": arguments,
    }


def test_model_gateway_planner_drives_full_typed_loop() -> None:
    provider = FakeLLMProvider(
        [
            RawProviderResponse(
                output=_tool_output(
                    "order_lookup",
                    {"order_id": "10086"},
                ),
                model="fake-agent-model",
                input_tokens=10,
                output_tokens=5,
            ),
            RawProviderResponse(
                output=_tool_output(
                    "logistics_lookup",
                    {"order_id": "10086"},
                ),
                model="fake-agent-model",
                input_tokens=10,
                output_tokens=5,
            ),
            RawProviderResponse(
                output=_tool_output(
                    "policy_lookup",
                    {"category": "not_received", "region": "CN"},
                ),
                model="fake-agent-model",
                input_tokens=10,
                output_tokens=5,
            ),
            RawProviderResponse(
                output={
                    "action": "finish",
                    "reason": "可信Observation已经满足完成条件",
                    "outcome": "human_review_required",
                    "final_summary": "签收争议证据齐全，转人工继续处理。",
                },
                model="fake-agent-model",
                input_tokens=10,
                output_tokens=5,
            ),
        ]
    )
    model_records = InMemoryModelCallRepository()
    planner = ModelGatewayAgentPlanner(
        ModelGateway(
            provider,
            model_records,
            model="fake-agent-model",
            reasoning_effort="low",
            max_output_tokens=500,
            max_attempts=2,
            retry_base_seconds=0.001,
            sleeper=lambda _: None,
        )
    )
    current_source = source()
    registry = ToolRegistry(build_after_sales_tools(current_source))
    tool_records = InMemoryToolCallRecorder()
    executor = ToolExecutor(registry, tool_records)
    try:
        state = AgentRunner(
            planner,
            registry,
            executor,
        ).run(command())
    finally:
        executor.close()

    assert state.status is AgentRunStatus.COMPLETED
    assert (
        state.final_outcome
        is InvestigationOutcome.HUMAN_REVIEW_REQUIRED
    )
    assert state.model_input_tokens == 40
    assert state.model_output_tokens == 20
    assert len(model_records.records) == 4
    assert len(tool_records.observations) == 3
    assert all(step.model_call_id is not None for step in state.steps)
    first_request = provider.requests[0]
    assert "allowed_tools" in first_request.input_text
    assert "order_lookup" in first_request.input_text
    assert "logistics_lookup" not in first_request.input_text
    assert "policy_lookup" not in first_request.input_text
    assert "payment and fulfillment" in provider.requests[0].input_text
    assert "delivery status" in provider.requests[1].input_text
    assert "evidence and approval" in provider.requests[1].input_text
    assert "evidence and approval" in provider.requests[2].input_text
    assert "delivery status" not in provider.requests[2].input_text
    assert "allowed_tools" not in provider.requests[3].input_text
    assert "<agent_runtime_data>" in first_request.input_text
    assert all(record.context_build_id is not None for record in model_records.records)

    corrected = replace(state, order_id="10087")
    assert relevant_tool_names(corrected) == frozenset({"order_lookup"})
    corrected_candidates = agent_context_candidates(
        corrected,
        registry.descriptors_for(command().actor),
        system_instructions=AGENT_PROMPT.instructions,
    )
    old_order_evidence = [
        item
        for item in corrected_candidates
        if item.source is ContextSource.TOOL_OBSERVATION
        and item.content["tool_name"] in {"order_lookup", "logistics_lookup"}
    ]
    assert old_order_evidence
    assert all(not item.eligible for item in old_order_evidence)
    assert all(
        item.exclusion_reason is ContextDecisionReason.INVALIDATED
        for item in old_order_evidence
    )
