from datetime import UTC, datetime, timedelta
from os import environ
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.approval import (
    RefundReviewAction,
    RefundReviewCommand,
)
from app.agent.checkpointing import postgres_checkpointer
from app.agent.errors import (
    RefundApprovalRequiredError,
    RefundReviewDeniedError,
)
from app.agent.models import (
    AgentActionKind,
    AgentDecision,
    AgentRunCommand,
    AgentRunStatus,
    AgentTerminationReason,
    AgentToolArguments,
    InvestigationOutcome,
    PlannerResult,
    RefundProposalCandidate,
)
from app.agent.workflow import DurableAgentWorkflow
from app.domain.auth import AuthenticatedActor, Role
from app.domain.investigation_triage import EvidenceKind
from app.domain.ticket import TicketCategory
from app.policy.refund import RefundProposalStatus
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
from app.tools.refund import (
    InMemoryRefundDataSource,
    RefundExecutionGuard,
    build_refund_tools,
)
from app.tools.registry import ToolRegistry

TENANT_ID = UUID("c0000000-0000-0000-0000-000000000001")
OTHER_TENANT_ID = UUID(
    "c0000000-0000-0000-0000-000000000002"
)
NOW = datetime(2026, 7, 27, 11, 0, tzinfo=UTC)


class RefundProposalPlanner:
    def decide(self, state, allowed_tools):
        if _latest(state, OrderObservation) is None:
            decision = _tool_decision(
                "order_lookup",
                order_id=state.order_id,
            )
        elif _latest(state, LogisticsObservation) is None:
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
        else:
            decision = AgentDecision(
                action=AgentActionKind.PROPOSE_REFUND,
                reason=(
                    "Evidence supports creating a candidate for backend "
                    "policy and independent human review."
                ),
                refund_proposal=RefundProposalCandidate(
                    amount_minor=12900,
                    currency="CNY",
                    reason=(
                        "Verified delivery dispute requires an approved "
                        "customer refund."
                    ),
                ),
            )
        return PlannerResult(
            decision=decision,
            input_tokens=8,
            output_tokens=4,
        )


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _agent() -> AuthenticatedActor:
    return AuthenticatedActor(
        actor_id="agent-requester",
        tenant_id=TENANT_ID,
        roles=frozenset({Role.AGENT}),
    )


def _reviewer(
    actor_id: str = "supervisor-reviewer",
    *,
    tenant_id: UUID = TENANT_ID,
    role: Role = Role.SUPERVISOR,
) -> AuthenticatedActor:
    return AuthenticatedActor(
        actor_id=actor_id,
        tenant_id=tenant_id,
        roles=frozenset({role}),
    )


def _command() -> AgentRunCommand:
    return AgentRunCommand(
        actor=_agent(),
        ticket_id=uuid4(),
        order_id="10086",
        category=TicketCategory.NOT_RECEIVED,
        goal="Investigate the disputed delivery and refund if safely approved.",
        request_id=f"request-{uuid4()}",
        trace_id=f"trace-{uuid4()}",
    )


def _evidence_source() -> InMemoryAfterSalesDataSource:
    return InMemoryAfterSalesDataSource(
        orders=[
            SimulatedOrder(
                tenant_id=TENANT_ID,
                order_id="10086",
                status=OrderStatus.SHIPPED,
                amount_minor=12900,
                currency="CNY",
                observed_at=NOW,
            )
        ],
        logistics=[
            SimulatedLogistics(
                tenant_id=TENANT_ID,
                order_id="10086",
                status=LogisticsStatus.DELIVERED,
                proof_available=True,
                signed_at=NOW,
                observed_at=NOW,
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
                summary="Proof review and human approval are mandatory.",
                effective_at=NOW,
            )
        ],
    )


def _workflow(
    saver,
    refund_source: InMemoryRefundDataSource,
    *,
    clock=None,
):
    evidence_source = _evidence_source()
    execution_guard = RefundExecutionGuard(
        b"day10-refund-workflow-test-secret"
    )
    registry = ToolRegistry(
        [
            *build_after_sales_tools(evidence_source),
            *build_refund_tools(
                refund_source,
                execution_guard,
            ),
        ]
    )
    recorder = InMemoryToolCallRecorder()
    executor = ToolExecutor(registry, recorder)
    workflow = DurableAgentWorkflow(
        RefundProposalPlanner(),
        registry,
        executor,
        saver,
        wall_clock=clock or (lambda: NOW),
        refund_execution_guard=execution_guard,
    )
    return workflow, executor, recorder, evidence_source


def _tool_decision(
    name: str,
    *,
    order_id: str | None = None,
    category: TicketCategory | None = None,
    region: str | None = None,
) -> AgentDecision:
    return AgentDecision(
        action=AgentActionKind.CALL_TOOL,
        reason="Collect the next required trusted observation.",
        tool_name=name,
        tool_version="1.0.0",
        arguments=AgentToolArguments(
            order_id=order_id,
            category=category,
            region=region,
        ),
    )


def _latest(state, output_type):
    for observation in reversed(state.observations):
        if observation.succeeded and isinstance(
            observation.output,
            output_type,
        ):
            return observation.output
    return None


def _review_command(
    view,
    action: RefundReviewAction,
    *,
    actor=None,
    modified_proposal=None,
) -> RefundReviewCommand:
    proposal = view.refund_proposal
    assert proposal is not None
    return RefundReviewCommand(
        actor=actor or _reviewer(),
        action=action,
        expected_proposal_version=proposal.version,
        expected_proposal_hash=proposal.proposal_hash,
        reason=f"Reviewer selected {action.value} after checking evidence.",
        modified_proposal=modified_proposal,
    )


def test_refund_always_pauses_before_the_critical_write() -> None:
    refund_source = InMemoryRefundDataSource()
    workflow, executor, _, evidence_source = _workflow(
        InMemorySaver(),
        refund_source,
    )
    try:
        paused = workflow.start(_command())
    finally:
        executor.close()

    assert paused.paused is True
    assert paused.state.status is AgentRunStatus.RUNNING
    assert paused.interrupt_payloads[0]["kind"] == "refund_approval"
    assert refund_source.execution_count == 0
    assert [item[0] for item in evidence_source.calls] == [
        "order_lookup",
        "logistics_lookup",
        "policy_lookup",
    ]
    with pytest.raises(RefundApprovalRequiredError):
        workflow.resume(paused.state.run_id)


def test_exact_approval_executes_once_and_verifies_result() -> None:
    refund_source = InMemoryRefundDataSource()
    workflow, executor, recorder, _ = _workflow(
        InMemorySaver(),
        refund_source,
    )
    try:
        paused = workflow.start(_command())
        completed = workflow.review_refund(
            paused.state.run_id,
            _review_command(paused, RefundReviewAction.APPROVE),
        )
    finally:
        executor.close()

    assert completed.state.status is AgentRunStatus.COMPLETED
    assert (
        completed.state.final_outcome
        is InvestigationOutcome.REFUND_COMPLETED
    )
    assert completed.refund_proposal is not None
    assert (
        completed.refund_proposal.status
        is RefundProposalStatus.EXECUTED
    )
    assert refund_source.execution_count == 1
    assert [item[0] for item in refund_source.calls] == [
        "refund_execute",
        "refund_status_lookup",
    ]
    assert recorder.observations[-2].tool_name == "refund_execute"
    assert recorder.observations[-1].tool_name == (
        "refund_status_lookup"
    )


@pytest.mark.parametrize(
    ("actor", "expected_code"),
    [
        (
            _reviewer(
                "agent-requester",
                role=Role.SUPERVISOR,
            ),
            "refund_review_four_eyes_required",
        ),
        (
            _reviewer("other-tenant", tenant_id=OTHER_TENANT_ID),
            "refund_review_cross_tenant",
        ),
        (
            _reviewer("ordinary-agent", role=Role.AGENT),
            "refund_review_permission_denied",
        ),
    ],
)
def test_invalid_reviewer_identity_cannot_cross_gate(
    actor,
    expected_code,
) -> None:
    refund_source = InMemoryRefundDataSource()
    workflow, executor, _, _ = _workflow(
        InMemorySaver(),
        refund_source,
    )
    try:
        paused = workflow.start(_command())
        with pytest.raises(RefundReviewDeniedError) as captured:
            workflow.review_refund(
                paused.state.run_id,
                _review_command(
                    paused,
                    RefundReviewAction.APPROVE,
                    actor=actor,
                ),
            )
        still_paused = workflow.get(paused.state.run_id)
    finally:
        executor.close()

    assert captured.value.error_code == expected_code
    assert still_paused.paused is True
    assert refund_source.execution_count == 0


def test_rejection_and_takeover_never_execute_refund() -> None:
    for action, expected_reason in [
        (
            RefundReviewAction.REJECT,
            AgentTerminationReason.APPROVAL_REJECTED,
        ),
        (
            RefundReviewAction.TAKEOVER,
            AgentTerminationReason.HUMAN_TAKEOVER,
        ),
    ]:
        refund_source = InMemoryRefundDataSource()
        workflow, executor, _, _ = _workflow(
            InMemorySaver(),
            refund_source,
        )
        try:
            paused = workflow.start(_command())
            ended = workflow.review_refund(
                paused.state.run_id,
                _review_command(paused, action),
            )
        finally:
            executor.close()
        assert ended.state.status is AgentRunStatus.ESCALATED
        assert ended.state.termination_reason is expected_reason
        assert refund_source.execution_count == 0


def test_modification_creates_new_version_and_needs_new_reviewer() -> None:
    refund_source = InMemoryRefundDataSource()
    workflow, executor, _, _ = _workflow(
        InMemorySaver(),
        refund_source,
    )
    modifier = _reviewer("supervisor-modifier")
    second_reviewer = _reviewer("supervisor-second-reviewer")
    try:
        first = workflow.start(_command())
        old_hash = first.refund_proposal.proposal_hash
        second = workflow.review_refund(
            first.state.run_id,
            _review_command(
                first,
                RefundReviewAction.MODIFY,
                actor=modifier,
                modified_proposal=RefundProposalCandidate(
                    amount_minor=9900,
                    currency="CNY",
                    reason="Reviewer reduced amount after evidence review.",
                ),
            ),
        )
        assert second.paused is True
        assert second.refund_proposal.version == 2
        assert second.refund_proposal.proposal_hash != old_hash
        assert len(second.refund_proposal_history) == 2
        assert (
            second.refund_proposal_history[0].status
            is RefundProposalStatus.SUPERSEDED
        )
        with pytest.raises(RefundReviewDeniedError) as stale:
            workflow.review_refund(
                second.state.run_id,
                RefundReviewCommand(
                    actor=second_reviewer,
                    action=RefundReviewAction.APPROVE,
                    expected_proposal_version=1,
                    expected_proposal_hash=old_hash,
                    reason="Attempt to use stale proposal approval.",
                ),
            )
        assert stale.value.error_code == (
            "refund_review_binding_mismatch"
        )
        with pytest.raises(RefundReviewDeniedError) as same_reviewer:
            workflow.review_refund(
                second.state.run_id,
                _review_command(
                    second,
                    RefundReviewAction.APPROVE,
                    actor=modifier,
                ),
            )
        assert same_reviewer.value.error_code == (
            "refund_review_modifier_cannot_approve"
        )
        completed = workflow.review_refund(
            second.state.run_id,
            _review_command(
                second,
                RefundReviewAction.APPROVE,
                actor=second_reviewer,
            ),
        )
    finally:
        executor.close()

    assert completed.state.status is AgentRunStatus.COMPLETED
    assert completed.refund_proposal.amount_minor == 9900
    assert refund_source.execution_count == 1


def test_expired_approval_fails_closed_without_write() -> None:
    clock = MutableClock(NOW)
    refund_source = InMemoryRefundDataSource()
    workflow, executor, _, _ = _workflow(
        InMemorySaver(),
        refund_source,
        clock=clock,
    )
    try:
        paused = workflow.start(_command())
        clock.value = NOW + timedelta(hours=25)
        expired = workflow.review_refund(
            paused.state.run_id,
            _review_command(paused, RefundReviewAction.APPROVE),
        )
    finally:
        executor.close()

    assert expired.state.status is AgentRunStatus.ESCALATED
    assert (
        expired.state.termination_reason
        is AgentTerminationReason.APPROVAL_EXPIRED
    )
    assert refund_source.execution_count == 0


def test_checkpoint_restart_preserves_gate_and_exact_binding() -> None:
    saver = InMemorySaver()
    refund_source = InMemoryRefundDataSource()
    first, first_executor, _, _ = _workflow(saver, refund_source)
    try:
        paused = first.start(_command())
    finally:
        first_executor.close()

    second, second_executor, _, _ = _workflow(saver, refund_source)
    try:
        recovered = second.get(paused.state.run_id)
        completed = second.review_refund(
            recovered.state.run_id,
            _review_command(
                recovered,
                RefundReviewAction.APPROVE,
            ),
        )
        with pytest.raises(RefundApprovalRequiredError):
            second.review_refund(
                completed.state.run_id,
                _review_command(
                    recovered,
                    RefundReviewAction.APPROVE,
                ),
            )
    finally:
        second_executor.close()

    assert recovered.paused is True
    assert completed.state.status is AgentRunStatus.COMPLETED
    assert refund_source.execution_count == 1


@pytest.mark.postgres
def test_postgres_restart_preserves_refund_approval_gate() -> None:
    database_url = environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is not configured.")
    run_id = uuid4()
    refund_source = InMemoryRefundDataSource()
    with postgres_checkpointer(database_url, setup=True) as saver:
        first, first_executor, _, _ = _workflow(
            saver,
            refund_source,
        )
        try:
            paused = first.start(_command(), run_id=run_id)
        finally:
            first_executor.close()
    assert paused.paused is True
    assert refund_source.execution_count == 0

    with postgres_checkpointer(database_url) as saver:
        second, second_executor, _, _ = _workflow(
            saver,
            refund_source,
        )
        try:
            recovered = second.get(run_id)
            completed = second.review_refund(
                run_id,
                _review_command(
                    recovered,
                    RefundReviewAction.APPROVE,
                ),
            )
        finally:
            second_executor.close()

    assert recovered.paused is True
    assert completed.state.status is AgentRunStatus.COMPLETED
    assert refund_source.execution_count == 1
