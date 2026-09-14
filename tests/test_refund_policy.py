from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.agent.models import (
    AgentRunState,
    AgentRunStatus,
    RefundProposalCandidate,
)
from app.domain.auth import AuthenticatedActor, Role
from app.domain.investigation_triage import EvidenceKind
from app.domain.ticket import TicketCategory
from app.policy.refund import RefundPolicyEngine
from app.tools.after_sales import (
    InMemoryAfterSalesDataSource,
    LogisticsStatus,
    OrderStatus,
    SimulatedLogistics,
    SimulatedOrder,
    SimulatedPolicy,
    build_after_sales_tools,
)
from app.tools.contracts import (
    ToolCallRequest,
    ToolExecutionContext,
)
from app.tools.executor import InMemoryToolCallRecorder, ToolExecutor
from app.tools.registry import ToolRegistry

TENANT_ID = UUID("a0000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)


def _runtime_state(
    *,
    order_status: OrderStatus = OrderStatus.SHIPPED,
    proof_available: bool = True,
    currency: str = "CNY",
) -> AgentRunState:
    source = InMemoryAfterSalesDataSource(
        orders=[
            SimulatedOrder(
                tenant_id=TENANT_ID,
                order_id="10086",
                status=order_status,
                amount_minor=12900,
                currency=currency,
                observed_at=NOW,
            )
        ],
        logistics=[
            SimulatedLogistics(
                tenant_id=TENANT_ID,
                order_id="10086",
                status=LogisticsStatus.DELIVERED,
                proof_available=proof_available,
                signed_at=NOW if proof_available else None,
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
                    EvidenceKind.PAYMENT_STATUS,
                    EvidenceKind.LOGISTICS_TRACE,
                    EvidenceKind.DELIVERY_PROOF,
                    EvidenceKind.APPLICABLE_POLICY,
                ),
                requires_human_approval=True,
                summary="Delivery disputes require proof and human approval.",
                effective_at=NOW,
            )
        ],
    )
    actor = AuthenticatedActor(
        actor_id="agent-refund-policy",
        tenant_id=TENANT_ID,
        roles=frozenset({Role.AGENT}),
    )
    registry = ToolRegistry(build_after_sales_tools(source))
    executor = ToolExecutor(registry, InMemoryToolCallRecorder())
    context = ToolExecutionContext(
        actor=actor,
        request_id="request-refund-policy",
        trace_id="trace-refund-policy",
        agent_run_id="run-refund-policy",
        agent_step_id="evidence",
    )
    try:
        observations = tuple(
            executor.execute(
                ToolCallRequest(
                    tool_name=name,
                    tool_version="1.0.0",
                    arguments=arguments,
                    context=context,
                )
            )
            for name, arguments in [
                ("order_lookup", {"order_id": "10086"}),
                ("logistics_lookup", {"order_id": "10086"}),
                (
                    "policy_lookup",
                    {
                        "category": "not_received",
                        "region": "CN",
                    },
                ),
            ]
        )
    finally:
        executor.close()
    return AgentRunState(
        run_id=uuid4(),
        tenant_id=TENANT_ID,
        actor_id=actor.actor_id,
        ticket_id=uuid4(),
        order_id="10086",
        category=TicketCategory.NOT_RECEIVED,
        goal="Investigate the delivery dispute and propose a safe refund.",
        request_id=context.request_id,
        trace_id=context.trace_id,
        max_steps=8,
        max_duration_seconds=30,
        max_total_tokens=5000,
        max_same_decision_attempts=2,
        status=AgentRunStatus.RUNNING,
        termination_reason=None,
        terminal_error_code=None,
        step_count=3,
        model_input_tokens=0,
        model_output_tokens=0,
        observations=observations,
        steps=(),
        verification_failures=(),
        final_outcome=None,
        final_summary=None,
        started_at=NOW,
        updated_at=NOW,
        completed_at=None,
    )


def _candidate(
    *,
    amount_minor: int = 12900,
    currency: str = "CNY",
) -> RefundProposalCandidate:
    return RefundProposalCandidate(
        amount_minor=amount_minor,
        currency=currency,
        reason="Customer disputes delivery after proof was investigated.",
    )


def test_policy_creates_backend_bound_proposal() -> None:
    state = _runtime_state()

    evaluation = RefundPolicyEngine().evaluate(
        state,
        _candidate(),
        now=NOW,
    )

    assert evaluation.allowed is True
    assert evaluation.proposal is not None
    assert evaluation.proposal.tenant_id == TENANT_ID
    assert evaluation.proposal.order_id == "10086"
    assert evaluation.proposal.amount_minor == 12900
    assert len(evaluation.proposal.proposal_hash) == 64
    assert evaluation.proposal.idempotency_key.startswith("refund-")


def test_policy_rejects_over_refund_and_currency_mismatch() -> None:
    state = _runtime_state()
    engine = RefundPolicyEngine()

    over = engine.evaluate(
        state,
        _candidate(amount_minor=12901),
        now=NOW,
    )
    wrong_currency = engine.evaluate(
        state,
        _candidate(currency="USD"),
        now=NOW,
    )

    assert over.code == "refund_amount_exceeds_order_total"
    assert wrong_currency.code == "refund_currency_mismatch"
    assert over.proposal is None
    assert wrong_currency.proposal is None


def test_policy_rejects_missing_proof_and_non_refundable_order() -> None:
    missing_proof = RefundPolicyEngine().evaluate(
        _runtime_state(proof_available=False),
        _candidate(),
        now=NOW,
    )
    cancelled = RefundPolicyEngine().evaluate(
        _runtime_state(order_status=OrderStatus.CANCELLED),
        _candidate(),
        now=NOW,
    )

    assert missing_proof.code == "refund_policy_evidence_unsatisfied"
    assert missing_proof.missing_evidence == ("delivery_proof",)
    assert cancelled.code == "order_not_refundable"


def test_policy_rejects_evidence_for_a_different_order() -> None:
    state = replace(_runtime_state(), order_id="10087")

    evaluation = RefundPolicyEngine().evaluate(
        state,
        _candidate(),
        now=NOW,
    )

    assert evaluation.code == "refund_required_evidence_missing"
    assert "order_status" in evaluation.missing_evidence
