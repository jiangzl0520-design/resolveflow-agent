from datetime import UTC, datetime, timedelta
from os import environ
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.checkpointing import postgres_checkpointer
from app.agent.errors import (
    AgentCheckpointCompatibilityError,
    AgentRunAlreadyExistsError,
)
from app.agent.models import (
    AgentActionKind,
    AgentBudget,
    AgentDecision,
    AgentRunCommand,
    AgentRunStatus,
    AgentTerminationReason,
    AgentToolArguments,
    InvestigationOutcome,
    PlannerResult,
)
from app.agent.workflow import DurableAgentWorkflow
from app.domain.auth import AuthenticatedActor, Role
from app.domain.investigation_triage import EvidenceKind
from app.domain.ticket import TicketCategory
from app.tools.after_sales import (
    InMemoryAfterSalesDataSource,
    LogisticsObservation,
    LogisticsStatus,
    OrderObservation,
    OrderStatus,
    PolicyObservation,
    SimulatedLogistics,
    SimulatedOrder,
    SimulatedPolicy,
    build_after_sales_tools,
)
from app.tools.executor import InMemoryToolCallRecorder, ToolExecutor
from app.tools.registry import ToolRegistry

TENANT_ID = UUID("90000000-0000-0000-0000-000000000001")
OBSERVED_AT = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)


class EvidenceDrivenPlanner:
    def decide(self, state, allowed_tools):
        order = _latest(state, OrderObservation)
        if order is None:
            decision = _tool_decision(
                "order_lookup",
                order_id=state.order_id,
            )
        elif order.status in {
            OrderStatus.CANCELLED,
            OrderStatus.PENDING_PAYMENT,
        }:
            decision = _finish(
                InvestigationOutcome.NO_REFUNDABLE_PAYMENT
            )
        else:
            logistics = _latest(state, LogisticsObservation)
            if logistics is None:
                decision = _tool_decision(
                    "logistics_lookup",
                    order_id=state.order_id,
                )
            elif _latest(state, PolicyObservation) is None:
                decision = _tool_decision(
                    "policy_lookup",
                    category=state.category,
                    region="CN",
                )
            elif logistics.status in {
                LogisticsStatus.CREATED,
                LogisticsStatus.IN_TRANSIT,
            }:
                decision = _finish(
                    InvestigationOutcome.DELIVERY_IN_PROGRESS
                )
            else:
                decision = _finish(
                    InvestigationOutcome.HUMAN_REVIEW_REQUIRED
                )
        return PlannerResult(
            decision=decision,
            input_tokens=7,
            output_tokens=3,
        )


def _actor() -> AuthenticatedActor:
    return AuthenticatedActor(
        actor_id="durable-agent-test",
        tenant_id=TENANT_ID,
        roles=frozenset({Role.AGENT}),
    )


def _command() -> AgentRunCommand:
    return AgentRunCommand(
        actor=_actor(),
        ticket_id=uuid4(),
        order_id="10086",
        category=TicketCategory.NOT_RECEIVED,
        goal="调查未收到包裹并收集可验证证据",
        request_id=f"request-{uuid4()}",
        trace_id=f"trace-{uuid4()}",
    )


def _source() -> InMemoryAfterSalesDataSource:
    return InMemoryAfterSalesDataSource(
        orders=[
            SimulatedOrder(
                tenant_id=TENANT_ID,
                order_id="10086",
                status=OrderStatus.SHIPPED,
                amount_minor=12900,
                currency="CNY",
                observed_at=OBSERVED_AT,
            )
        ],
        logistics=[
            SimulatedLogistics(
                tenant_id=TENANT_ID,
                order_id="10086",
                status=LogisticsStatus.DELIVERED,
                proof_available=True,
                signed_at=OBSERVED_AT,
                observed_at=OBSERVED_AT,
            )
        ],
        policies=[
            SimulatedPolicy(
                policy_id="CN-NOT-RECEIVED-001",
                version="2026.07",
                category=TicketCategory.NOT_RECEIVED,
                region="CN",
                required_evidence=(
                    EvidenceKind.ORDER_STATUS,
                    EvidenceKind.LOGISTICS_TRACE,
                    EvidenceKind.DELIVERY_PROOF,
                    EvidenceKind.APPLICABLE_POLICY,
                ),
                requires_human_approval=True,
                summary="签收争议必须核验签收证明并转人工审核。",
                effective_at=OBSERVED_AT,
            )
        ],
    )


def _workflow(source, saver):
    registry = ToolRegistry(build_after_sales_tools(source))
    recorder = InMemoryToolCallRecorder()
    executor = ToolExecutor(registry, recorder)
    workflow = DurableAgentWorkflow(
        EvidenceDrivenPlanner(),
        registry,
        executor,
        saver,
    )
    return workflow, executor, recorder


def _tool_decision(
    name: str,
    *,
    order_id: str | None = None,
    category: TicketCategory | None = None,
    region: str | None = None,
) -> AgentDecision:
    return AgentDecision(
        action=AgentActionKind.CALL_TOOL,
        reason="收集下一项可信证据",
        tool_name=name,
        tool_version="1.0.0",
        arguments=AgentToolArguments(
            order_id=order_id,
            category=category,
            region=region,
        ),
    )


def _finish(outcome: InvestigationOutcome) -> AgentDecision:
    return AgentDecision(
        action=AgentActionKind.FINISH,
        reason="可信证据已满足确定性完成条件",
        outcome=outcome,
        final_summary="证据收集完成，已得到可验证调查结论。",
    )


def _latest(state, output_type):
    for observation in reversed(state.observations):
        if observation.succeeded and isinstance(
            observation.output,
            output_type,
        ):
            return observation.output
    return None


def test_langgraph_workflow_completes_with_checkpoint_history() -> None:
    current_source = _source()
    workflow, executor, _ = _workflow(
        current_source,
        InMemorySaver(),
    )
    try:
        view = workflow.start(_command())
    finally:
        executor.close()

    assert view.state.status is AgentRunStatus.COMPLETED
    assert (
        view.state.final_outcome
        is InvestigationOutcome.HUMAN_REVIEW_REQUIRED
    )
    assert view.next_nodes == ()
    assert view.paused is False
    assert [item[0] for item in current_source.calls] == [
        "order_lookup",
        "logistics_lookup",
        "policy_lookup",
    ]
    assert workflow.checkpoint_count(view.state.run_id) >= 10


def test_langgraph_blocks_cross_order_tool_arguments_before_execution() -> None:
    class AttackedPlanner:
        def __init__(self) -> None:
            self.calls = 0
            self.safe = EvidenceDrivenPlanner()

        def decide(self, state, allowed_tools):
            self.calls += 1
            if self.calls == 1:
                return PlannerResult(
                    decision=_tool_decision(
                        "order_lookup",
                        order_id="10087",
                    )
                )
            return self.safe.decide(state, allowed_tools)

    current_source = _source()
    registry = ToolRegistry(build_after_sales_tools(current_source))
    recorder = InMemoryToolCallRecorder()
    executor = ToolExecutor(registry, recorder)
    workflow = DurableAgentWorkflow(
        AttackedPlanner(),
        registry,
        executor,
        InMemorySaver(),
    )
    try:
        view = workflow.start(_command())
    finally:
        executor.close()

    assert view.state.status is AgentRunStatus.COMPLETED
    assert view.state.verification_failures[0] == (
        "agent_tool_resource_binding_mismatch"
    )
    assert view.state.steps[0].tool_call_id is None
    assert [item[0] for item in current_source.calls] == [
        "order_lookup",
        "logistics_lookup",
        "policy_lookup",
    ]


def test_pause_resume_does_not_repeat_checkpointed_tool() -> None:
    current_source = _source()
    workflow, executor, _ = _workflow(
        current_source,
        InMemorySaver(),
    )
    try:
        paused = workflow.start(
            _command(),
            pause_after_step=1,
        )
        assert paused.paused is True
        assert paused.state.status is AgentRunStatus.RUNNING
        assert [item[0] for item in current_source.calls] == [
            "order_lookup"
        ]
        assert paused.interrupt_payloads[0]["last_tool"] == (
            "order_lookup"
        )

        completed = workflow.resume(paused.state.run_id)
    finally:
        executor.close()

    assert completed.state.status is AgentRunStatus.COMPLETED
    assert [item[0] for item in current_source.calls] == [
        "order_lookup",
        "logistics_lookup",
        "policy_lookup",
    ]


def test_cancel_at_pause_is_terminal_and_skips_remaining_tools() -> None:
    current_source = _source()
    workflow, executor, _ = _workflow(
        current_source,
        InMemorySaver(),
    )
    try:
        paused = workflow.start(
            _command(),
            pause_after_step=1,
        )
        cancelled = workflow.cancel(paused.state.run_id)
    finally:
        executor.close()

    assert cancelled.state.status is AgentRunStatus.CANCELLED
    assert (
        cancelled.state.termination_reason
        is AgentTerminationReason.CANCELLED
    )
    assert cancelled.next_nodes == ()
    assert [item[0] for item in current_source.calls] == [
        "order_lookup"
    ]


def test_invalid_resume_action_fails_closed() -> None:
    workflow, executor, _ = _workflow(
        _source(),
        InMemorySaver(),
    )
    try:
        paused = workflow.start(
            _command(),
            pause_after_step=1,
        )
        failed = workflow.resume(
            paused.state.run_id,
            action="skip_safety",
        )
    finally:
        executor.close()

    assert failed.state.status is AgentRunStatus.FAILED
    assert failed.state.terminal_error_code == "invalid_resume_action"


def test_run_id_cannot_silently_overwrite_existing_thread() -> None:
    workflow, executor, _ = _workflow(
        _source(),
        InMemorySaver(),
    )
    run_id = uuid4()
    try:
        workflow.start(_command(), run_id=run_id)
        with pytest.raises(AgentRunAlreadyExistsError):
            workflow.start(_command(), run_id=run_id)
    finally:
        executor.close()


def test_recovery_fails_if_checkpointed_tool_version_is_missing() -> None:
    saver = InMemorySaver()
    workflow, executor, _ = _workflow(_source(), saver)
    try:
        paused = workflow.start(
            _command(),
            pause_after_step=1,
        )
    finally:
        executor.close()

    empty_registry = ToolRegistry([])
    empty_executor = ToolExecutor(
        empty_registry,
        InMemoryToolCallRecorder(),
    )
    incompatible = DurableAgentWorkflow(
        EvidenceDrivenPlanner(),
        empty_registry,
        empty_executor,
        saver,
    )
    try:
        with pytest.raises(AgentCheckpointCompatibilityError):
            incompatible.get(paused.state.run_id)
    finally:
        empty_executor.close()


def test_human_pause_does_not_consume_execution_time_budget() -> None:
    saver = InMemorySaver()
    first_source = _source()
    first_registry = ToolRegistry(
        build_after_sales_tools(first_source)
    )
    first_executor = ToolExecutor(
        first_registry,
        InMemoryToolCallRecorder(),
    )
    try:
        first = DurableAgentWorkflow(
            EvidenceDrivenPlanner(),
            first_registry,
            first_executor,
            saver,
            budget=AgentBudget(max_duration_seconds=30),
            wall_clock=lambda: OBSERVED_AT,
        )
        paused = first.start(
            _command(),
            pause_after_step=1,
        )
    finally:
        first_executor.close()

    second_source = _source()
    second_registry = ToolRegistry(
        build_after_sales_tools(second_source)
    )
    second_executor = ToolExecutor(
        second_registry,
        InMemoryToolCallRecorder(),
    )
    try:
        second = DurableAgentWorkflow(
            EvidenceDrivenPlanner(),
            second_registry,
            second_executor,
            saver,
            budget=AgentBudget(max_duration_seconds=30),
            wall_clock=lambda: OBSERVED_AT + timedelta(seconds=31),
        )
        completed = second.resume(paused.state.run_id)
    finally:
        second_executor.close()

    assert completed.state.status is AgentRunStatus.COMPLETED
    assert [item[0] for item in second_source.calls] == [
        "logistics_lookup",
        "policy_lookup",
    ]


def test_active_time_budget_is_enforced_after_resume() -> None:
    saver = InMemorySaver()
    first_source = _source()
    first_registry = ToolRegistry(
        build_after_sales_tools(first_source)
    )
    first_executor = ToolExecutor(
        first_registry,
        InMemoryToolCallRecorder(),
    )
    try:
        first = DurableAgentWorkflow(
            EvidenceDrivenPlanner(),
            first_registry,
            first_executor,
            saver,
            budget=AgentBudget(max_duration_seconds=30),
            wall_clock=lambda: OBSERVED_AT,
        )
        paused = first.start(
            _command(),
            pause_after_step=1,
        )
    finally:
        first_executor.close()

    class ResumeClock:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self) -> datetime:
            self.calls += 1
            seconds = 31 if self.calls == 1 else 62
            return OBSERVED_AT + timedelta(seconds=seconds)

    second_source = _source()
    second_registry = ToolRegistry(
        build_after_sales_tools(second_source)
    )
    second_executor = ToolExecutor(
        second_registry,
        InMemoryToolCallRecorder(),
    )
    try:
        second = DurableAgentWorkflow(
            EvidenceDrivenPlanner(),
            second_registry,
            second_executor,
            saver,
            budget=AgentBudget(max_duration_seconds=30),
            wall_clock=ResumeClock(),
        )
        failed = second.resume(paused.state.run_id)
    finally:
        second_executor.close()

    assert failed.state.status is AgentRunStatus.FAILED
    assert (
        failed.state.termination_reason
        is AgentTerminationReason.TIME_BUDGET_EXCEEDED
    )
    assert second_source.calls == []


@pytest.mark.postgres
def test_postgres_checkpoint_survives_runtime_recreation() -> None:
    database_url = environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is not configured.")
    run_id = uuid4()
    first_source = _source()
    with postgres_checkpointer(database_url, setup=True) as saver:
        first, first_executor, _ = _workflow(first_source, saver)
        try:
            paused = first.start(
                _command(),
                run_id=run_id,
                pause_after_step=1,
            )
        finally:
            first_executor.close()
    assert paused.paused is True
    assert [item[0] for item in first_source.calls] == [
        "order_lookup"
    ]

    second_source = _source()
    with postgres_checkpointer(database_url) as saver:
        second, second_executor, _ = _workflow(second_source, saver)
        try:
            recovered = second.get(run_id)
            completed = second.resume(run_id)
        finally:
            second_executor.close()

    assert recovered.paused is True
    assert completed.state.status is AgentRunStatus.COMPLETED
    assert [item[0] for item in second_source.calls] == [
        "logistics_lookup",
        "policy_lookup",
    ]
