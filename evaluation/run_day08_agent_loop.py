from argparse import ArgumentParser
from datetime import UTC, datetime
import json
from pathlib import Path
import platform
from uuid import UUID, uuid4

from app.agent.models import (
    AgentActionKind,
    AgentDecision,
    AgentRunCommand,
    AgentRunStatus,
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
from app.tools.contracts import (
    ToolCallRequest,
    ToolExecutionContext,
)
from app.tools.executor import InMemoryToolCallRecorder, ToolExecutor
from app.tools.registry import ToolRegistry

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = (
    ROOT / "evaluation" / "datasets" / "day08_agent_loop_v1.json"
)
TENANT_ID = UUID("80000000-0000-0000-0000-000000000001")
OBSERVED_AT = datetime(2026, 7, 25, 10, 0, tzinfo=UTC)


def _actor() -> AuthenticatedActor:
    return AuthenticatedActor(
        actor_id="day08-evaluation-agent",
        tenant_id=TENANT_ID,
        roles=frozenset({Role.AGENT}),
    )


def _source(scenario: str) -> InMemoryAfterSalesDataSource:
    order_status = (
        OrderStatus.CANCELLED
        if scenario == "cancelled_order"
        else OrderStatus.SHIPPED
    )
    logistics_status = (
        LogisticsStatus.IN_TRANSIT
        if scenario == "delivery_in_transit"
        else LogisticsStatus.DELIVERED
    )
    logistics = []
    if order_status is not OrderStatus.CANCELLED:
        logistics.append(
            SimulatedLogistics(
                tenant_id=TENANT_ID,
                order_id="10086",
                status=logistics_status,
                proof_available=(
                    logistics_status is LogisticsStatus.DELIVERED
                ),
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
                order_id="10086",
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
                summary="签收争议需要可信证据和人工审核。",
                effective_at=OBSERVED_AT,
            )
        ],
    )
    if scenario == "transient_logistics_failure":
        source.fail_next("logistics_lookup")
    return source


def _tool_decision(
    name: str,
    *,
    order_id=None,
    category=None,
    region=None,
) -> AgentDecision:
    return AgentDecision(
        action=AgentActionKind.CALL_TOOL,
        reason="选择下一项缺失的可信证据",
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
        reason="当前Observation已满足确定性完成条件",
        outcome=outcome,
        final_summary="合成评估场景已获得足够的可信调查证据。",
    )


class ObservationDrivenPlanner:
    def decide(self, state, allowed_tools):
        order = _latest(state, OrderObservation)
        if order is None:
            decision = _tool_decision(
                "order_lookup",
                order_id=state.order_id,
            )
        elif order.status is OrderStatus.CANCELLED:
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
            elif logistics.status is LogisticsStatus.IN_TRANSIT:
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


def _latest(state, output_type):
    for observation in reversed(state.observations):
        if observation.succeeded and isinstance(
            observation.output,
            output_type,
        ):
            return observation.output
    return None


def _command(case_id: str) -> AgentRunCommand:
    return AgentRunCommand(
        actor=_actor(),
        ticket_id=uuid4(),
        order_id="10086",
        category=TicketCategory.NOT_RECEIVED,
        goal="调查未收到包裹并收集可信证据",
        request_id=f"request-{case_id}",
        trace_id=f"trace-{case_id}",
    )


def _run_agent(
    scenario: str,
    case_id: str,
) -> tuple[bool, str | None, int, int]:
    source = _source(scenario)
    registry = ToolRegistry(build_after_sales_tools(source))
    recorder = InMemoryToolCallRecorder()
    executor = ToolExecutor(registry, recorder)
    try:
        state = AgentRunner(
            ObservationDrivenPlanner(),
            registry,
            executor,
        ).run(_command(case_id))
    finally:
        executor.close()
    return (
        state.status is AgentRunStatus.COMPLETED,
        (
            state.final_outcome.value
            if state.final_outcome is not None
            else None
        ),
        len(recorder.observations),
        state.step_count,
    )


def _run_fixed_pipeline(
    scenario: str,
    case_id: str,
) -> tuple[bool, str | None, int, int]:
    source = _source(scenario)
    registry = ToolRegistry(build_after_sales_tools(source))
    recorder = InMemoryToolCallRecorder()
    executor = ToolExecutor(registry, recorder)
    actor = _actor()
    observations = []
    try:
        for step, (tool_name, arguments) in enumerate(
            [
                ("order_lookup", {"order_id": "10086"}),
                ("logistics_lookup", {"order_id": "10086"}),
                (
                    "policy_lookup",
                    {"category": "not_received", "region": "CN"},
                ),
            ],
            start=1,
        ):
            observation = executor.execute(
                ToolCallRequest(
                    tool_name=tool_name,
                    tool_version="1.0.0",
                    arguments=arguments,
                    context=ToolExecutionContext(
                        actor=actor,
                        request_id=f"baseline-request-{case_id}",
                        trace_id=f"baseline-trace-{case_id}",
                        agent_run_id=f"baseline-run-{case_id}",
                        agent_step_id=f"step-{step}",
                    ),
                )
            )
            observations.append(observation)
            if not observation.succeeded:
                return False, None, len(observations), len(observations)
    finally:
        executor.close()

    logistics = next(
        item.output
        for item in observations
        if isinstance(item.output, LogisticsObservation)
    )
    outcome = (
        InvestigationOutcome.DELIVERY_IN_PROGRESS
        if logistics.status is LogisticsStatus.IN_TRANSIT
        else InvestigationOutcome.HUMAN_REVIEW_REQUIRED
    )
    return True, outcome.value, len(observations), len(observations)


def run(dataset_path: Path) -> dict[str, object]:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    expected_outcomes = dataset["expected_outcomes"]
    baseline_completed = 0
    baseline_correct = 0
    baseline_calls = 0
    baseline_unnecessary_calls = 0
    baseline_transient_recovered = 0
    agent_completed = 0
    agent_correct = 0
    agent_calls = 0
    agent_steps = 0
    agent_unnecessary_calls = 0
    agent_transient_recovered = 0
    case_count = 0

    for scenario, count in dataset["scenario_counts"].items():
        for index in range(count):
            case_count += 1
            case_id = f"{scenario}-{index:02d}"
            baseline = _run_fixed_pipeline(scenario, case_id)
            current = _run_agent(scenario, case_id)
            expected = expected_outcomes[scenario]
            baseline_completed += baseline[0]
            baseline_correct += baseline[1] == expected
            baseline_calls += baseline[2]
            agent_completed += current[0]
            agent_correct += current[1] == expected
            agent_calls += current[2]
            agent_steps += current[3]
            if scenario == "cancelled_order":
                baseline_unnecessary_calls += max(
                    0,
                    baseline[2] - 1,
                )
                agent_unnecessary_calls += max(0, current[2] - 1)
            if scenario == "transient_logistics_failure":
                baseline_transient_recovered += baseline[0]
                agent_transient_recovered += current[0]

    return {
        "report_id": "day08-agent-loop-v1",
        "dataset": {
            **dataset,
            "case_count": case_count,
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "planner": "deterministic_observation_driven",
            "tool_provider": "InMemoryAfterSalesDataSource",
            "paid_api_calls": 0,
        },
        "fixed_three_tool_pipeline_baseline": {
            "completed_runs": baseline_completed,
            "correct_outcomes": baseline_correct,
            "tool_calls": baseline_calls,
            "unnecessary_calls_after_terminal_order_fact": (
                baseline_unnecessary_calls
            ),
            "transient_failures_recovered": (
                baseline_transient_recovered
            ),
        },
        "resolveflow_minimal_agent_loop": {
            "completed_runs": agent_completed,
            "correct_outcomes": agent_correct,
            "tool_calls": agent_calls,
            "agent_steps": agent_steps,
            "unnecessary_calls_after_terminal_order_fact": (
                agent_unnecessary_calls
            ),
            "transient_failures_recovered": agent_transient_recovered,
        },
        "interpretation_limits": [
            "All business facts, failures, and planner decisions are synthetic.",
            "The planner is deterministic; this does not measure real-LLM semantic quality.",
            "This measures branching, recovery, verification, and bounded execution rather than production business value.",
            "No production latency, token cost, or human-intervention rate is claimed.",
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
        arguments.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)


if __name__ == "__main__":
    main()
