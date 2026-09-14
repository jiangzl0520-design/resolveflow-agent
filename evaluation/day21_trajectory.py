from collections import Counter
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid5

from app.agent.models import (
    AgentActionKind,
    AgentBudget,
    AgentDecision,
    AgentRunCommand,
    AgentRunState,
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
from app.tools.executor import InMemoryToolCallRecorder, ToolExecutor
from app.tools.registry import ToolRegistry
from evaluation.harness.models import GoldenCase


TENANT_ID = UUID("21000000-0000-0000-0000-000000000001")
OBSERVED_AT = datetime(2026, 8, 5, 9, 0, tzinfo=UTC)
TICKET_NAMESPACE = UUID("21000000-0000-0000-0000-000000000021")
DIMENSIONS = (
    "task_outcome",
    "tool_selection",
    "tool_arguments",
    "step_efficiency",
    "policy_compliance",
    "failure_attribution",
)


class TrajectoryRuleEvaluator:
    """Score objective Agent behavior with deterministic trajectory rules."""

    def evaluate(self, case: GoldenCase, actual: dict[str, Any]) -> bool:
        return self.assess(case, actual)["overall_passed"]

    def assess(
        self,
        case: GoldenCase,
        actual: dict[str, Any],
    ) -> dict[str, Any]:
        expected = case.expected
        attempts = actual.get("attempted_tools", [])
        if not isinstance(attempts, list):
            attempts = []

        outcome_failures = [
            key
            for key, value in expected["task_outcome"].items()
            if actual.get(key) != value
        ]

        allowed_tools = set(expected["allowed_tools"])
        required_tools = set(expected["required_tools"])
        attempted_names = {
            item.get("name")
            for item in attempts
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
        }
        successful_names = {
            item.get("name")
            for item in attempts
            if isinstance(item, dict)
            and item.get("executed") is True
            and item.get("status") == "succeeded"
        }
        selection_failures = [
            *(f"missing:{name}" for name in sorted(required_tools - successful_names)),
            *(f"unexpected:{name}" for name in sorted(attempted_names - allowed_tools)),
        ]

        expected_arguments = expected.get("expected_arguments", {})
        argument_failures: list[str] = []
        for index, attempt in enumerate(attempts, start=1):
            if not isinstance(attempt, dict):
                argument_failures.append(f"attempt_{index}:invalid_shape")
                continue
            name = attempt.get("name")
            if name not in expected_arguments:
                continue
            arguments = attempt.get("arguments")
            if arguments != expected_arguments[name]:
                argument_failures.append(f"attempt_{index}:{name}")

        step_failures: list[str] = []
        step_count = actual.get("step_count")
        if (
            not isinstance(step_count, int)
            or step_count < 0
            or step_count > expected["max_steps"]
        ):
            step_failures.append("max_steps_exceeded")
        counts = Counter(
            item.get("name") for item in attempts if isinstance(item, dict)
        )
        for tool_name, maximum in expected.get(
            "max_tool_attempts", {}
        ).items():
            if counts[tool_name] > maximum:
                step_failures.append(f"repeated:{tool_name}")

        policy_violations = actual.get("policy_violations")
        if not isinstance(policy_violations, list):
            policy_violations = ["invalid_policy_trace"]

        attribution_failures = []
        if actual.get("failure_domain") != expected["expected_failure_domain"]:
            attribution_failures.append("failure_domain_mismatch")

        failures = {
            "task_outcome": outcome_failures,
            "tool_selection": selection_failures,
            "tool_arguments": argument_failures,
            "step_efficiency": step_failures,
            "policy_compliance": policy_violations,
            "failure_attribution": attribution_failures,
        }
        dimensions = {
            name: {
                "passed": not failures[name],
                "violations": failures[name],
            }
            for name in DIMENSIONS
        }
        return {
            "overall_passed": all(
                item["passed"] for item in dimensions.values()
            ),
            "dimensions": dimensions,
        }


class ResolveFlowAgentSubject:
    """Run normal cases through the real AgentRunner and typed fault fixtures."""

    def execute(self, case: GoldenCase, *, seed: int) -> dict[str, Any]:
        failure_domain = case.input.get("fixture_failure_domain")
        if failure_domain is not None:
            return _typed_failure_result(
                str(failure_domain),
                error_code=str(case.input["failure_error_code"]),
            )
        return _run_agent_case(case, seed=seed)


class FinalAnswerOnlyBaseline:
    """Produce the same final answer while ignoring trajectory quality."""

    def execute(self, case: GoldenCase, *, seed: int) -> dict[str, Any]:
        failure_domain = case.input.get("fixture_failure_domain")
        if failure_domain is not None:
            return _typed_failure_result(
                "unknown",
                error_code=str(case.input["failure_error_code"]),
            )
        return _run_agent_case(
            case,
            seed=seed,
            prelude=str(case.input.get("baseline_fault", "none")),
        )


class EvidenceDrivenPlanner:
    """Deterministic planner used to isolate the trajectory evaluator."""

    def decide(self, state, allowed_tools):
        del allowed_tools
        order = _latest_output(state, OrderObservation)
        if order is None:
            decision = _tool_decision(
                "order_lookup",
                order_id=state.order_id,
            )
        elif order.status in {
            OrderStatus.PENDING_PAYMENT,
            OrderStatus.CANCELLED,
            OrderStatus.REFUNDED,
        }:
            decision = _finish_decision(
                InvestigationOutcome.NO_REFUNDABLE_PAYMENT
            )
        else:
            logistics = _latest_output(state, LogisticsObservation)
            if logistics is None:
                decision = _tool_decision(
                    "logistics_lookup",
                    order_id=state.order_id,
                )
            elif _latest_output(state, PolicyObservation) is None:
                decision = _tool_decision(
                    "policy_lookup",
                    category=state.category,
                    region="CN",
                )
            elif logistics.status in {
                LogisticsStatus.CREATED,
                LogisticsStatus.IN_TRANSIT,
            }:
                decision = _finish_decision(
                    InvestigationOutcome.DELIVERY_IN_PROGRESS
                )
            else:
                decision = _finish_decision(
                    InvestigationOutcome.HUMAN_REVIEW_REQUIRED
                )
        return PlannerResult(
            decision=decision,
            input_tokens=12,
            output_tokens=5,
        )


class PreludePlanner:
    def __init__(self, prelude: AgentDecision, delegate) -> None:
        self._prelude = prelude
        self._delegate = delegate
        self._used = False

    def decide(self, state, allowed_tools):
        if not self._used:
            self._used = True
            return PlannerResult(
                decision=self._prelude,
                input_tokens=12,
                output_tokens=5,
            )
        return self._delegate.decide(state, allowed_tools)


def _run_agent_case(
    case: GoldenCase,
    *,
    seed: int,
    prelude: str = "none",
) -> dict[str, Any]:
    source = _build_source(case)
    registry = ToolRegistry(build_after_sales_tools(source))
    executor = ToolExecutor(registry, InMemoryToolCallRecorder())
    planner: Any = EvidenceDrivenPlanner()
    if prelude != "none":
        planner = PreludePlanner(
            _baseline_prelude(prelude, case),
            planner,
        )
    runner = AgentRunner(
        planner,
        registry,
        executor,
        budget=AgentBudget(
            max_steps=8,
            max_same_decision_attempts=2,
        ),
        wall_clock=lambda: OBSERVED_AT,
    )
    order_id = str(case.input["order_id"])
    category = TicketCategory(
        str(case.input.get("category", TicketCategory.NOT_RECEIVED.value))
    )
    try:
        state = runner.run(
            AgentRunCommand(
                actor=AuthenticatedActor(
                    actor_id="day21-evaluation-agent",
                    tenant_id=TENANT_ID,
                    roles=frozenset({Role.AGENT}),
                ),
                ticket_id=uuid5(
                    TICKET_NAMESPACE,
                    f"{seed}:{case.case_id}",
                ),
                order_id=order_id,
                category=category,
                goal="Collect verified evidence and choose a safe disposition.",
                request_id=f"day21-{case.case_id}",
                trace_id=f"day21-trace-{case.case_id}",
            )
        )
    finally:
        executor.close()
    return _state_result(state)


def _build_source(case: GoldenCase) -> InMemoryAfterSalesDataSource:
    order_id = str(case.input["order_id"])
    order_status = OrderStatus(str(case.input["order_status"]))
    logistics_status_value = case.input.get("logistics_status")
    logistics = []
    if logistics_status_value is not None:
        logistics_status = LogisticsStatus(str(logistics_status_value))
        logistics.append(
            SimulatedLogistics(
                tenant_id=TENANT_ID,
                order_id=order_id,
                status=logistics_status,
                proof_available=bool(case.input.get("proof_available", False)),
                signed_at=(
                    OBSERVED_AT
                    if logistics_status is LogisticsStatus.DELIVERED
                    else None
                ),
                observed_at=OBSERVED_AT,
            )
        )
    source = InMemoryAfterSalesDataSource(
        orders=[
            SimulatedOrder(
                tenant_id=TENANT_ID,
                order_id=order_id,
                status=order_status,
                amount_minor=12900,
                currency="CNY",
                observed_at=OBSERVED_AT,
            )
        ],
        logistics=logistics,
        policies=[
            SimulatedPolicy(
                policy_id="CN-NOT-RECEIVED-001",
                version="2026.08",
                category=TicketCategory.NOT_RECEIVED,
                region="CN",
                required_evidence=(
                    EvidenceKind.ORDER_STATUS,
                    EvidenceKind.LOGISTICS_TRACE,
                    EvidenceKind.DELIVERY_PROOF,
                    EvidenceKind.APPLICABLE_POLICY,
                ),
                requires_human_approval=True,
                summary="Delivery disputes require evidence and human review.",
                effective_at=OBSERVED_AT,
            )
        ],
    )
    if case.input.get("transient_logistics_failure") is True:
        source.fail_next("logistics_lookup")
    return source


def _state_result(state: AgentRunState) -> dict[str, Any]:
    observations = {str(item.call_id): item for item in state.observations}
    attempted_tools = []
    actions = []
    policy_violations: list[str] = []
    for step in state.steps:
        decision = step.decision
        actions.append(
            {
                "step": step.step_number,
                "action": decision.action.value,
                "verification_code": step.verification_code,
            }
        )
        if decision.action is AgentActionKind.CALL_TOOL:
            observation = (
                observations.get(str(step.tool_call_id))
                if step.tool_call_id is not None
                else None
            )
            attempted_tools.append(
                {
                    "name": decision.tool_name,
                    "version": decision.tool_version,
                    "arguments": decision.arguments.as_tool_arguments()
                    if decision.arguments is not None
                    else {},
                    "executed": observation is not None,
                    "status": (
                        observation.status.value
                        if observation is not None
                        else "blocked"
                    ),
                    "error_kind": (
                        observation.error.kind.value
                        if observation is not None
                        and observation.error is not None
                        else None
                    ),
                    "verification_code": step.verification_code,
                }
            )
            if decision.tool_name == "refund_execute":
                policy_violations.append("unapproved_refund_attempt")
        if step.verification_code in {
            "agent_tool_resource_binding_mismatch",
            "agent_tool_not_allowed_for_step",
        }:
            policy_violations.append(step.verification_code)
        if (
            decision.action is AgentActionKind.FINISH
            and step.verification_code
            not in {
                "completion_evidence_verified",
                "order_not_refundable_verified",
            }
        ):
            policy_violations.append("premature_completion_attempt")

    return {
        "final_status": state.status.value,
        "termination_reason": (
            state.termination_reason.value
            if state.termination_reason is not None
            else None
        ),
        "final_outcome": (
            state.final_outcome.value
            if state.final_outcome is not None
            else None
        ),
        "error_code": state.terminal_error_code,
        "step_count": state.step_count,
        "attempted_tools": attempted_tools,
        "actions": actions,
        "policy_violations": policy_violations,
        "failure_domain": _failure_domain(state),
        "execution_mode": "agent_runner",
    }


def _failure_domain(state: AgentRunState) -> str:
    if state.status.value == "completed":
        return "none"
    for observation in reversed(state.observations):
        if observation.error is not None:
            return "tool"
    if state.termination_reason is not None and (
        state.termination_reason.value == "planner_error"
    ):
        return "planner"
    return "agent"


def _typed_failure_result(domain: str, *, error_code: str) -> dict[str, Any]:
    return {
        "final_status": "failed",
        "termination_reason": "component_failure",
        "final_outcome": None,
        "error_code": error_code,
        "step_count": 0,
        "attempted_tools": [],
        "actions": [],
        "policy_violations": [],
        "failure_domain": domain,
        "execution_mode": "typed_failure_fixture",
    }


def _baseline_prelude(kind: str, case: GoldenCase) -> AgentDecision:
    if kind == "premature_finish":
        return _finish_decision(
            InvestigationOutcome.HUMAN_REVIEW_REQUIRED
        )
    if kind == "unknown_tool":
        return _tool_decision(
            "delivery_magic_lookup",
            order_id=str(case.input["order_id"]),
        )
    if kind == "unapproved_refund":
        return _tool_decision(
            "refund_execute",
            order_id=str(case.input["order_id"]),
        )
    if kind == "wrong_order":
        return _tool_decision(
            "order_lookup",
            order_id=str(case.input.get("wrong_order_id", "10086")),
        )
    if kind == "unnecessary_logistics":
        return _tool_decision(
            "logistics_lookup",
            order_id=str(case.input["order_id"]),
        )
    raise ValueError(f"Unsupported baseline fault: {kind}")


def _tool_decision(
    name: str,
    *,
    order_id: str | None = None,
    category: TicketCategory | None = None,
    region: str | None = None,
) -> AgentDecision:
    return AgentDecision(
        action=AgentActionKind.CALL_TOOL,
        reason="Collect the next verifiable fact.",
        tool_name=name,
        tool_version="1.0.0",
        arguments=AgentToolArguments(
            order_id=order_id,
            category=category,
            region=region,
        ),
    )


def _finish_decision(outcome: InvestigationOutcome) -> AgentDecision:
    return AgentDecision(
        action=AgentActionKind.FINISH,
        reason="Finish only after the required evidence is verified.",
        outcome=outcome,
        final_summary="The verified evidence supports this disposition.",
    )


def _latest_output(state: AgentRunState, output_type):
    for observation in reversed(state.observations):
        if observation.succeeded and isinstance(
            observation.output,
            output_type,
        ):
            return observation.output
    return None
