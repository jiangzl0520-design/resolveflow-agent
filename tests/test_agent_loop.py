from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.agent.errors import AgentPlannerError
from app.agent.fake_planner import ScriptedAgentPlanner
from app.agent.planner import AGENT_PROMPT
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
from app.agent.runner import AgentRunner
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
from app.tools.executor import (
    InMemoryToolCallRecorder,
    ToolExecutor,
)
from app.tools.contracts import ToolFailureKind
from app.tools.registry import ToolRegistry

TENANT_ID = UUID("80000000-0000-0000-0000-000000000001")
OBSERVED_AT = datetime(2026, 7, 25, 10, 0, tzinfo=UTC)


def actor(role: Role = Role.AGENT) -> AuthenticatedActor:
    return AuthenticatedActor(
        actor_id="agent-loop-test",
        tenant_id=TENANT_ID,
        roles=frozenset({role}),
    )


def command(
    *,
    current_actor=None,
    order_id: str = "10086",
) -> AgentRunCommand:
    return AgentRunCommand(
        actor=current_actor or actor(),
        ticket_id=uuid4(),
        order_id=order_id,
        category=TicketCategory.NOT_RECEIVED,
        goal="调查包裹未收到问题并收集可信证据",
        request_id="request-agent-loop",
        trace_id="trace-agent-loop",
    )


def source(
    *,
    order_status: OrderStatus = OrderStatus.SHIPPED,
    logistics_status: LogisticsStatus = LogisticsStatus.DELIVERED,
    proof_available: bool = True,
    include_wrong_order: bool = False,
) -> InMemoryAfterSalesDataSource:
    orders = [
        SimulatedOrder(
            tenant_id=TENANT_ID,
            order_id="10086",
            status=order_status,
            amount_minor=12900,
            currency="CNY",
            observed_at=OBSERVED_AT,
        )
    ]
    if include_wrong_order:
        orders.append(
            SimulatedOrder(
                tenant_id=TENANT_ID,
                order_id="10087",
                status=OrderStatus.CANCELLED,
                amount_minor=8000,
                currency="CNY",
                observed_at=OBSERVED_AT,
            )
        )
    logistics = []
    if order_status not in {
        OrderStatus.CANCELLED,
        OrderStatus.PENDING_PAYMENT,
    }:
        logistics.append(
            SimulatedLogistics(
                tenant_id=TENANT_ID,
                order_id="10086",
                status=logistics_status,
                proof_available=proof_available,
                signed_at=(
                    OBSERVED_AT
                    if logistics_status is LogisticsStatus.DELIVERED
                    else None
                ),
                observed_at=OBSERVED_AT,
            )
        )
    return InMemoryAfterSalesDataSource(
        orders=orders,
        logistics=logistics,
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
                summary="签收争议需要核验签收证明并转人工审核。",
                effective_at=OBSERVED_AT,
            )
        ],
    )


def tool_decision(
    name: str,
    *,
    order_id: str | None = None,
    category: TicketCategory | None = None,
    region: str | None = None,
    reason: str = "收集下一项可信证据",
) -> AgentDecision:
    return AgentDecision(
        action=AgentActionKind.CALL_TOOL,
        reason=reason,
        tool_name=name,
        tool_version="1.0.0",
        arguments=AgentToolArguments(
            order_id=order_id,
            category=category,
            region=region,
        ),
    )


def finish_decision(
    outcome: InvestigationOutcome,
    summary: str = "证据已满足当前调查的完成条件",
) -> AgentDecision:
    return AgentDecision(
        action=AgentActionKind.FINISH,
        reason="根据已验证Observation结束当前调查",
        outcome=outcome,
        final_summary=summary,
    )


def escalate_decision() -> AgentDecision:
    return AgentDecision(
        action=AgentActionKind.ESCALATE,
        reason="当前信息不足且无法安全自动继续",
        escalation_reason="需要人工核验不完整或冲突的证据",
    )


class EvidenceDrivenPlanner:
    def decide(self, state, allowed_tools):
        order = _latest(state, OrderObservation)
        if order is None:
            decision = tool_decision(
                "order_lookup",
                order_id=state.order_id,
            )
        elif order.status in {
            OrderStatus.CANCELLED,
            OrderStatus.PENDING_PAYMENT,
        }:
            decision = finish_decision(
                InvestigationOutcome.NO_REFUNDABLE_PAYMENT
            )
        else:
            logistics = _latest(state, LogisticsObservation)
            if logistics is None:
                decision = tool_decision(
                    "logistics_lookup",
                    order_id=state.order_id,
                )
            elif _latest(state, PolicyObservation) is None:
                decision = tool_decision(
                    "policy_lookup",
                    category=state.category,
                    region="CN",
                )
            elif logistics.status in {
                LogisticsStatus.CREATED,
                LogisticsStatus.IN_TRANSIT,
            }:
                decision = finish_decision(
                    InvestigationOutcome.DELIVERY_IN_PROGRESS
                )
            else:
                decision = finish_decision(
                    InvestigationOutcome.HUMAN_REVIEW_REQUIRED
                )
        return PlannerResult(
            decision=decision,
            input_tokens=7,
            output_tokens=3,
        )


def _latest(state, output_type):
    for observation in reversed(state.observations):
        if observation.succeeded and isinstance(
            observation.output,
            output_type,
        ):
            return observation.output
    return None


def run_agent(
    planner,
    current_source,
    *,
    budget=None,
    current_actor=None,
    monotonic_clock=None,
):
    registry = ToolRegistry(build_after_sales_tools(current_source))
    recorder = InMemoryToolCallRecorder()
    executor = ToolExecutor(registry, recorder)
    options = {}
    if monotonic_clock is not None:
        options["monotonic_clock"] = monotonic_clock
    runner = AgentRunner(
        planner,
        registry,
        executor,
        budget=budget,
        **options,
    )
    try:
        state = runner.run(
            command(current_actor=current_actor)
        )
    finally:
        executor.close()
    return state, recorder


def test_observations_drive_different_valid_paths() -> None:
    delivered, delivered_recorder = run_agent(
        EvidenceDrivenPlanner(),
        source(),
    )
    cancelled, cancelled_recorder = run_agent(
        EvidenceDrivenPlanner(),
        source(order_status=OrderStatus.CANCELLED),
    )

    assert delivered.status is AgentRunStatus.COMPLETED
    assert (
        delivered.final_outcome
        is InvestigationOutcome.HUMAN_REVIEW_REQUIRED
    )
    assert [item.tool_name for item in delivered.observations] == [
        "order_lookup",
        "logistics_lookup",
        "policy_lookup",
    ]
    assert cancelled.status is AgentRunStatus.COMPLETED
    assert (
        cancelled.final_outcome
        is InvestigationOutcome.NO_REFUNDABLE_PAYMENT
    )
    assert [item.tool_name for item in cancelled.observations] == [
        "order_lookup"
    ]
    assert len(delivered_recorder.observations) == 3
    assert len(cancelled_recorder.observations) == 1


def test_verifier_rejects_premature_finish_then_allows_repair() -> None:
    planner = ScriptedAgentPlanner(
        [
            finish_decision(
                InvestigationOutcome.HUMAN_REVIEW_REQUIRED
            ),
            tool_decision("order_lookup", order_id="10086"),
            tool_decision("logistics_lookup", order_id="10086"),
            tool_decision(
                "policy_lookup",
                category=TicketCategory.NOT_RECEIVED,
                region="CN",
            ),
            finish_decision(
                InvestigationOutcome.HUMAN_REVIEW_REQUIRED
            ),
        ]
    )

    state, _ = run_agent(planner, source())

    assert state.status is AgentRunStatus.COMPLETED
    assert state.verification_failures == (
        "missing_order_observation",
    )
    assert state.steps[0].verification_code == (
        "missing_order_observation"
    )
    assert state.steps[-1].verification_code == (
        "completion_evidence_verified"
    )


def test_retryable_dependency_error_can_be_replanned_and_recovered() -> None:
    current_source = source()
    current_source.fail_next("logistics_lookup")

    state, _ = run_agent(
        EvidenceDrivenPlanner(),
        current_source,
    )

    assert state.status is AgentRunStatus.COMPLETED
    logistics = [
        item
        for item in state.observations
        if item.tool_name == "logistics_lookup"
    ]
    assert len(logistics) == 2
    assert logistics[0].error is not None
    assert logistics[0].error.retryable is True
    assert logistics[1].succeeded is True


def test_agent_can_repair_selection_and_argument_errors() -> None:
    planner = ScriptedAgentPlanner(
        [
            tool_decision("invented_lookup", order_id="10086"),
            tool_decision("order_lookup"),
            tool_decision("order_lookup", order_id="10086"),
            finish_decision(
                InvestigationOutcome.NO_REFUNDABLE_PAYMENT
            ),
        ]
    )

    state, _ = run_agent(
        planner,
        source(order_status=OrderStatus.CANCELLED),
    )

    assert state.status is AgentRunStatus.COMPLETED
    assert state.observations[0].error is not None
    assert (
        state.observations[0].error.kind
        is ToolFailureKind.SELECTION
    )
    assert state.observations[1].error is not None
    assert (
        state.observations[1].error.kind
        is ToolFailureKind.ARGUMENT
    )
    assert state.observations[2].succeeded is True


def test_third_identical_decision_is_stopped_as_loop() -> None:
    repeated = tool_decision(
        "invented_lookup",
        order_id="10086",
    )
    planner = ScriptedAgentPlanner(
        [repeated, repeated, repeated]
    )

    state, _ = run_agent(planner, source())

    assert state.status is AgentRunStatus.FAILED
    assert (
        state.termination_reason
        is AgentTerminationReason.LOOP_DETECTED
    )
    assert len(state.observations) == 2
    assert state.step_count == 3
    assert state.steps[-1].verification_code == (
        "repeated_decision_detected"
    )


def test_max_steps_stops_non_repeating_bad_plan() -> None:
    planner = ScriptedAgentPlanner(
        [
            tool_decision("invented_one", order_id="10086"),
            tool_decision("invented_two", order_id="10086"),
        ]
    )

    state, _ = run_agent(
        planner,
        source(),
        budget=AgentBudget(max_steps=2),
    )

    assert state.status is AgentRunStatus.FAILED
    assert (
        state.termination_reason
        is AgentTerminationReason.MAX_STEPS_EXCEEDED
    )
    assert state.step_count == 2


def test_token_budget_stops_before_tool_execution() -> None:
    planner = ScriptedAgentPlanner(
        [
            PlannerResult(
                decision=tool_decision(
                    "order_lookup",
                    order_id="10086",
                ),
                input_tokens=70,
                output_tokens=40,
            )
        ]
    )

    state, recorder = run_agent(
        planner,
        source(),
        budget=AgentBudget(max_total_tokens=100),
    )

    assert state.status is AgentRunStatus.FAILED
    assert (
        state.termination_reason
        is AgentTerminationReason.TOKEN_BUDGET_EXCEEDED
    )
    assert recorder.observations == []


class SequenceClock:
    def __init__(self, values):
        self.values = iter(values)

    def __call__(self):
        return next(self.values)


def test_time_budget_stops_before_next_planning_step() -> None:
    planner = ScriptedAgentPlanner(
        [
            tool_decision("order_lookup", order_id="10086"),
        ]
    )

    state, _ = run_agent(
        planner,
        source(),
        budget=AgentBudget(max_duration_seconds=1),
        monotonic_clock=SequenceClock([0, 0, 2]),
    )

    assert state.status is AgentRunStatus.FAILED
    assert (
        state.termination_reason
        is AgentTerminationReason.TIME_BUDGET_EXCEEDED
    )
    assert len(state.observations) == 1


def test_permission_denial_escalates_without_replanning() -> None:
    planner = ScriptedAgentPlanner(
        [tool_decision("order_lookup", order_id="10086")]
    )

    state, _ = run_agent(
        planner,
        source(),
        current_actor=actor(Role.CUSTOMER),
    )

    assert state.status is AgentRunStatus.ESCALATED
    assert (
        state.termination_reason
        is AgentTerminationReason.TOOL_PERMISSION_DENIED
    )
    assert state.terminal_error_code == "tool_permission_denied"
    assert state.observations[0].error is not None


def test_planner_failure_is_terminal_and_observable() -> None:
    state, _ = run_agent(
        ScriptedAgentPlanner(
            [AgentPlannerError("model_output_invalid")]
        ),
        source(),
    )

    assert state.status is AgentRunStatus.FAILED
    assert (
        state.termination_reason
        is AgentTerminationReason.PLANNER_ERROR
    )
    assert state.terminal_error_code == "model_output_invalid"
    assert state.step_count == 0


def test_observation_for_wrong_order_cannot_satisfy_verifier() -> None:
    planner = ScriptedAgentPlanner(
        [
            tool_decision("order_lookup", order_id="10087"),
            finish_decision(
                InvestigationOutcome.NO_REFUNDABLE_PAYMENT
            ),
            escalate_decision(),
        ]
    )

    state, _ = run_agent(
        planner,
        source(include_wrong_order=True),
    )

    assert state.status is AgentRunStatus.ESCALATED
    assert state.verification_failures == (
        "agent_tool_resource_binding_mismatch",
        "missing_order_observation",
    )
    assert state.observations == ()
    assert state.steps[0].verification_code == (
        "agent_tool_resource_binding_mismatch"
    )


def test_tool_hidden_for_current_step_is_not_executed() -> None:
    planner = ScriptedAgentPlanner(
        [
            tool_decision("logistics_lookup", order_id="10086"),
            tool_decision("order_lookup", order_id="10086"),
            tool_decision("logistics_lookup", order_id="10086"),
            tool_decision(
                "policy_lookup",
                category=TicketCategory.NOT_RECEIVED,
                region="CN",
            ),
            finish_decision(
                InvestigationOutcome.HUMAN_REVIEW_REQUIRED
            ),
        ]
    )

    state, recorder = run_agent(planner, source())

    assert state.status is AgentRunStatus.COMPLETED
    assert state.verification_failures[0] == (
        "agent_tool_not_allowed_for_step"
    )
    assert state.steps[0].tool_call_id is None
    assert [item.tool_name for item in recorder.observations] == [
        "order_lookup",
        "logistics_lookup",
        "policy_lookup",
    ]


def test_system_instruction_leak_is_blocked_before_terminal_output() -> None:
    protected_sentence = AGENT_PROMPT.instructions.split(". ", maxsplit=1)[0]
    leaked = AgentDecision(
        action=AgentActionKind.ESCALATE,
        reason="外部内容要求输出隐藏规则",
        escalation_reason=protected_sentence,
    )
    planner = ScriptedAgentPlanner(
        [
            leaked,
            tool_decision("order_lookup", order_id="10086"),
            tool_decision("logistics_lookup", order_id="10086"),
            tool_decision(
                "policy_lookup",
                category=TicketCategory.NOT_RECEIVED,
                region="CN",
            ),
            finish_decision(
                InvestigationOutcome.HUMAN_REVIEW_REQUIRED
            ),
        ]
    )

    state, _ = run_agent(planner, source())

    assert state.status is AgentRunStatus.COMPLETED
    assert state.verification_failures[0] == (
        "agent_output_security_blocked"
    )
    assert state.steps[0].verification_code == (
        "agent_output_security_blocked"
    )
    assert protected_sentence not in (state.final_summary or "")
