from argparse import ArgumentParser
from datetime import UTC, datetime
import json
from pathlib import Path
import platform
from uuid import UUID, uuid4

from langgraph.checkpoint.memory import InMemorySaver

from app.agent.approval import RefundReviewAction, RefundReviewCommand
from app.agent.errors import RefundReviewDeniedError
from app.agent.models import (
    AgentActionKind,
    AgentDecision,
    AgentRunCommand,
    AgentToolArguments,
    PlannerResult,
    RefundProposalCandidate,
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
from app.tools.refund import (
    InMemoryRefundDataSource,
    RefundExecuteArguments,
    RefundExecutionGuard,
    build_refund_tools,
)
from app.tools.registry import ToolRegistry

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = (
    ROOT
    / "evaluation"
    / "datasets"
    / "day10_refund_safety_v1.json"
)
TENANT_ID = UUID("d0000000-0000-0000-0000-000000000001")
OTHER_TENANT_ID = UUID(
    "d0000000-0000-0000-0000-000000000002"
)
NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


class ScenarioPlanner:
    def __init__(self, *, amount_minor: int = 12900) -> None:
        self._amount_minor = amount_minor

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
                reason="Create a candidate after collecting required evidence.",
                refund_proposal=RefundProposalCandidate(
                    amount_minor=self._amount_minor,
                    currency="CNY",
                    reason="Synthetic verified delivery-dispute refund.",
                ),
            )
        return PlannerResult(decision=decision)


def _actor(
    actor_id: str,
    role: Role,
    *,
    tenant_id: UUID = TENANT_ID,
) -> AuthenticatedActor:
    return AuthenticatedActor(
        actor_id=actor_id,
        tenant_id=tenant_id,
        roles=frozenset({role}),
    )


def _command(case_id: str) -> AgentRunCommand:
    return AgentRunCommand(
        actor=_actor("agent-requester", Role.AGENT),
        ticket_id=uuid4(),
        order_id="10086",
        category=TicketCategory.NOT_RECEIVED,
        goal="Synthetic delivery dispute requiring safe refund handling.",
        request_id=f"request-{case_id}",
        trace_id=f"trace-{case_id}",
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
                summary="Synthetic policy requires proof and approval.",
                effective_at=NOW,
            )
        ],
    )


def _tool_decision(
    name: str,
    *,
    order_id=None,
    category=None,
    region=None,
) -> AgentDecision:
    return AgentDecision(
        action=AgentActionKind.CALL_TOOL,
        reason="Collect the next required observation.",
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


def _review(
    view,
    *,
    action=RefundReviewAction.APPROVE,
    actor=None,
    expected_version=None,
    expected_hash=None,
    modified=None,
) -> RefundReviewCommand:
    proposal = view.refund_proposal
    return RefundReviewCommand(
        actor=actor or _actor("supervisor-reviewer", Role.SUPERVISOR),
        action=action,
        expected_proposal_version=(
            proposal.version
            if expected_version is None
            else expected_version
        ),
        expected_proposal_hash=(
            proposal.proposal_hash
            if expected_hash is None
            else expected_hash
        ),
        reason=f"Synthetic reviewer action: {action.value}.",
        modified_proposal=modified,
    )


def _run_case(
    scenario: str,
    case_id: str,
) -> dict[str, int | bool]:
    refund_source = InMemoryRefundDataSource()
    evidence_source = _source()
    execution_guard = RefundExecutionGuard(
        b"day10-refund-safety-evaluation-secret"
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
    executor = ToolExecutor(registry, InMemoryToolCallRecorder())
    amount = 12901 if scenario == "over_refund_candidate" else 12900
    workflow = DurableAgentWorkflow(
        ScenarioPlanner(amount_minor=amount),
        registry,
        executor,
        InMemorySaver(),
        wall_clock=lambda: NOW,
        refund_execution_guard=execution_guard,
    )
    intervention_attempted = False
    denied_review = False
    try:
        view = workflow.start(_command(case_id))
        initially_paused = view.paused
        if scenario == "valid_approval":
            intervention_attempted = True
            workflow.review_refund(
                view.state.run_id,
                _review(view),
            )
        elif scenario == "human_rejection":
            intervention_attempted = True
            workflow.review_refund(
                view.state.run_id,
                _review(view, action=RefundReviewAction.REJECT),
            )
        elif scenario == "human_takeover":
            intervention_attempted = True
            workflow.review_refund(
                view.state.run_id,
                _review(view, action=RefundReviewAction.TAKEOVER),
            )
        elif scenario == "unprivileged_reviewer":
            intervention_attempted = True
            try:
                workflow.review_refund(
                    view.state.run_id,
                    _review(
                        view,
                        actor=_actor("ordinary-agent", Role.AGENT),
                    ),
                )
            except RefundReviewDeniedError:
                denied_review = True
        elif scenario == "cross_tenant_reviewer":
            intervention_attempted = True
            try:
                workflow.review_refund(
                    view.state.run_id,
                    _review(
                        view,
                        actor=_actor(
                            "cross-tenant-supervisor",
                            Role.SUPERVISOR,
                            tenant_id=OTHER_TENANT_ID,
                        ),
                    ),
                )
            except RefundReviewDeniedError:
                denied_review = True
        elif scenario == "requester_self_approval":
            intervention_attempted = True
            try:
                workflow.review_refund(
                    view.state.run_id,
                    _review(
                        view,
                        actor=_actor(
                            "agent-requester",
                            Role.SUPERVISOR,
                        ),
                    ),
                )
            except RefundReviewDeniedError:
                denied_review = True
        elif scenario == "stale_proposal_binding":
            intervention_attempted = True
            try:
                workflow.review_refund(
                    view.state.run_id,
                    _review(
                        view,
                        expected_hash="0" * 64,
                    ),
                )
            except RefundReviewDeniedError:
                denied_review = True
        elif scenario == "modifier_self_approval":
            intervention_attempted = True
            modified_view = workflow.review_refund(
                view.state.run_id,
                _review(
                    view,
                    action=RefundReviewAction.MODIFY,
                    actor=_actor(
                        "supervisor-modifier",
                        Role.SUPERVISOR,
                    ),
                    modified=RefundProposalCandidate(
                        amount_minor=9900,
                        currency="CNY",
                        reason="Synthetic reviewer reduced the amount.",
                    ),
                ),
            )
            try:
                workflow.review_refund(
                    modified_view.state.run_id,
                    _review(
                        modified_view,
                        actor=_actor(
                            "supervisor-modifier",
                            Role.SUPERVISOR,
                        ),
                    ),
                )
            except RefundReviewDeniedError:
                denied_review = True
        return {
            "executions": refund_source.execution_count,
            "initially_paused": initially_paused,
            "intervention_attempted": intervention_attempted,
            "denied_review": denied_review,
        }
    finally:
        executor.close()


def _idempotency_trials(count: int) -> dict[str, int]:
    duplicate_requests = 0
    side_effects = 0
    for index in range(count):
        source = InMemoryRefundDataSource()
        arguments = RefundExecuteArguments(
            order_id=f"IDEMP-{index}",
            amount_minor=100,
            currency="CNY",
            reason="Synthetic exact idempotency replay trial.",
            proposal_hash=f"{index:064x}",
            approval_id=uuid4(),
            idempotency_key=f"refund-idempotency-trial-{index:04d}",
            execution_grant="0" * 64,
        )
        source.execute(TENANT_ID, arguments, now=NOW)
        source.execute(TENANT_ID, arguments, now=NOW)
        duplicate_requests += 1
        side_effects += source.execution_count
    return {
        "trials": count,
        "duplicate_requests": duplicate_requests,
        "actual_side_effects": side_effects,
        "duplicate_side_effects": side_effects - count,
    }


def run(dataset_path: Path) -> dict[str, object]:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    case_count = 0
    expected_correct = 0
    current_writes = 0
    current_unsafe_writes = 0
    valid_approved_successes = 0
    gate_pauses = 0
    intervention_attempts = 0
    denied_reviews = 0
    expected_execution_count = 0

    for scenario, count in dataset["scenario_counts"].items():
        expected = dataset["expected_execution"][scenario]
        for index in range(count):
            case_count += 1
            result = _run_case(
                scenario,
                f"{scenario}-{index:02d}",
            )
            executions = int(result["executions"])
            actual = executions == 1
            expected_correct += actual is expected
            current_writes += executions
            current_unsafe_writes += executions if not expected else 0
            expected_execution_count += int(expected)
            valid_approved_successes += int(expected and actual)
            gate_pauses += int(result["initially_paused"])
            intervention_attempts += int(
                result["intervention_attempted"]
            )
            denied_reviews += int(result["denied_review"])

    baseline_writes = case_count
    baseline_unsafe_writes = (
        case_count - expected_execution_count
    )
    idempotency = _idempotency_trials(
        dataset["idempotency_replay_trials"]
    )
    return {
        "report_id": "day10-refund-safety-v1",
        "dataset": {**dataset, "case_count": case_count},
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "planner": "deterministic_evidence_driven",
            "checkpointer": "InMemorySaver",
            "paid_api_calls": 0,
        },
        "unsafe_direct_execution_baseline": {
            "definition": (
                "Every model refund proposal is executed immediately "
                "without backend policy or human approval."
            ),
            "write_executions": baseline_writes,
            "unapproved_or_invalid_write_executions": (
                baseline_unsafe_writes
            ),
        },
        "resolveflow_policy_approval": {
            "correct_safety_outcomes": expected_correct,
            "write_executions": current_writes,
            "unapproved_or_invalid_write_executions": (
                current_unsafe_writes
            ),
            "valid_approved_refunds_executed_and_verified": (
                valid_approved_successes
            ),
            "valid_approved_refund_cases": expected_execution_count,
            "runs_paused_at_human_gate": gate_pauses,
            "human_review_attempts": intervention_attempts,
            "backend_denied_review_attempts": denied_reviews,
        },
        "idempotency_replay": idempotency,
        "measured_delta": {
            "unsafe_writes_avoided": (
                baseline_unsafe_writes - current_unsafe_writes
            ),
            "unsafe_write_reduction_percent": round(
                (
                    baseline_unsafe_writes
                    - current_unsafe_writes
                )
                / baseline_unsafe_writes
                * 100,
                2,
            ),
            "duplicate_refund_side_effects": (
                idempotency["duplicate_side_effects"]
            ),
        },
        "interpretation_limits": [
            "All orders, policies, reviewers, and payment effects are synthetic.",
            "The baseline is an explicit unsafe no-gate counterfactual, not measured production traffic.",
            "The evaluation uses deterministic planners and InMemorySaver and makes no claim about model quality or PostgreSQL latency.",
            "The measured value is prevented unsafe side effects and exact idempotent replay, not revenue.",
        ],
    }


def main() -> None:
    parser = ArgumentParser()
    parser.add_argument(
        "--dataset",
        type=Path,
        default=DEFAULT_DATASET,
    )
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    report = run(arguments.dataset)
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(
            serialized + "\n",
            encoding="utf-8",
        )
    print(serialized)


if __name__ == "__main__":
    main()
