from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from app.agent.models import AgentRunCommand, AgentRunStatus
from app.agent.planner import ModelGatewayAgentPlanner
from app.agent.runner import AgentRunner
from app.context_engineering.builder import ContextBuilder
from app.context_engineering.tokens import TiktokenTokenCounter
from app.domain.auth import AuthenticatedActor, Role
from app.domain.context import ContextDecisionReason
from app.domain.memory import MemorySourceType, MemoryWriteCommand
from app.domain.ticket import TicketCategory
from app.llm.contracts import RawProviderResponse
from app.llm.fake_provider import FakeLLMProvider
from app.llm.gateway import ModelGateway
from app.memory.errors import MemoryReadError
from app.repositories.authorization_audit_repository import (
    InMemoryAuthorizationAuditRepository,
)
from app.repositories.context_trace_repository import (
    InMemoryContextTraceRepository,
)
from app.repositories.memory_repository import InMemoryMemoryRepository
from app.repositories.model_call_repository import InMemoryModelCallRepository
from app.services.authorization_service import AuthorizationService
from app.services.memory_service import MemoryService
from app.tools.after_sales import build_after_sales_tools
from app.tools.executor import InMemoryToolCallRecorder, ToolExecutor
from app.tools.registry import ToolRegistry
from tests.test_agent_loop import source

TENANT_ID = UUID("96000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 8, 5, 10, 0, tzinfo=UTC)


def _actor() -> AuthenticatedActor:
    return AuthenticatedActor(
        actor_id="memory-context-agent",
        tenant_id=TENANT_ID,
        roles=frozenset({Role.AGENT}),
    )


def _command(ticket_id) -> AgentRunCommand:
    return AgentRunCommand(
        actor=_actor(),
        ticket_id=ticket_id,
        order_id="10086",
        category=TicketCategory.NOT_RECEIVED,
        goal="Investigate the delivery dispute.",
        request_id=f"request-{ticket_id}",
        trace_id=f"trace-{ticket_id}",
    )


def _escalation() -> RawProviderResponse:
    return RawProviderResponse(
        output={
            "action": "escalate",
            "reason": "Evidence collection has not started.",
            "escalation_reason": "Synthetic one-step memory context test.",
        },
        model="fake-memory-model",
        input_tokens=20,
        output_tokens=10,
    )


def test_same_subject_memory_is_available_in_two_agent_runs() -> None:
    memory_repository = InMemoryMemoryRepository()
    MemoryService(
        memory_repository,
        AuthorizationService(InMemoryAuthorizationAuditRepository()),
        clock=lambda: NOW,
    ).remember(
        _actor(),
        MemoryWriteCommand(
            subject_id="customer-shared",
            memory_key="preference.language",
            value="zh-CN",
            source_type=MemorySourceType.EXPLICIT_USER,
            source_reference="message-language",
            confidence=0.99,
            observed_at=NOW,
            expires_at=NOW + timedelta(days=30),
            idempotency_key="shared-language",
            request_id="request-shared-language",
            trace_id="trace-shared-language",
        ),
    )
    ticket_a = uuid4()
    ticket_b = uuid4()
    memory_repository.bind_ticket(TENANT_ID, ticket_a, "customer-shared")
    memory_repository.bind_ticket(TENANT_ID, ticket_b, "customer-shared")
    provider = FakeLLMProvider([_escalation(), _escalation()])
    planner = ModelGatewayAgentPlanner(
        ModelGateway(
            provider,
            InMemoryModelCallRepository(),
            model="fake-memory-model",
            reasoning_effort="low",
            max_output_tokens=300,
            max_attempts=1,
            retry_base_seconds=0.001,
        ),
        memory_reader=memory_repository,
        clock=lambda: NOW,
    )
    registry = ToolRegistry(build_after_sales_tools(source()))
    executor = ToolExecutor(registry, InMemoryToolCallRecorder())
    try:
        first = AgentRunner(planner, registry, executor).run(_command(ticket_a))
        second = AgentRunner(planner, registry, executor).run(_command(ticket_b))
    finally:
        executor.close()

    assert first.status is AgentRunStatus.ESCALATED
    assert second.status is AgentRunStatus.ESCALATED
    assert len(provider.requests) == 2
    assert all('"source":"memory"' in item.input_text for item in provider.requests)
    assert all('"value":"zh-cn"' in item.input_text for item in provider.requests)


def test_memory_dependency_failure_is_traced_but_does_not_block_safe_task() -> None:
    class FailingMemoryReader:
        def list_active_for_ticket(self, tenant_id, ticket_id, *, at):
            raise MemoryReadError()

    traces = InMemoryContextTraceRepository()
    provider = FakeLLMProvider([_escalation()])
    planner = ModelGatewayAgentPlanner(
        ModelGateway(
            provider,
            InMemoryModelCallRepository(),
            model="fake-memory-model",
            reasoning_effort="low",
            max_output_tokens=300,
            max_attempts=1,
            retry_base_seconds=0.001,
        ),
        context_builder=ContextBuilder(
            TiktokenTokenCounter("fake-memory-model"),
            traces,
        ),
        memory_reader=FailingMemoryReader(),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    registry = ToolRegistry(build_after_sales_tools(source()))
    executor = ToolExecutor(registry, InMemoryToolCallRecorder())
    try:
        state = AgentRunner(planner, registry, executor).run(
            _command(uuid4())
        )
    finally:
        executor.close()

    assert state.status is AgentRunStatus.ESCALATED
    memory_trace = next(
        item
        for item in traces.traces
        if item.fragment_id == "memory:retrieval-status"
    )
    assert not memory_trace.included
    assert (
        memory_trace.decision_reason
        is ContextDecisionReason.DEPENDENCY_UNAVAILABLE
    )
