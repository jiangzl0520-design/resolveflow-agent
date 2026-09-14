from argparse import ArgumentParser
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import UTC, datetime
import json
from pathlib import Path
import platform
from uuid import UUID

from app.domain.auth import AuthenticatedActor, Role
from app.domain.investigation_triage import EvidenceKind
from app.domain.ticket import TicketCategory
from app.tools.after_sales import (
    InMemoryAfterSalesDataSource,
    LogisticsStatus,
    OrderStatus,
    SimulatedLogistics,
    SimulatedOrder,
    SimulatedPolicy,
    build_after_sales_tools,
)
from app.tools.contracts import ToolCallRequest, ToolExecutionContext
from app.tools.executor import InMemoryToolCallRecorder, ToolExecutor
from app.tools.registry import ToolRegistry

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = (
    ROOT / "evaluation" / "datasets" / "day07_tool_contracts_v1.json"
)
TENANT_ID = UUID("70000000-0000-0000-0000-000000000001")
OBSERVED_AT = datetime(2026, 7, 25, 9, 0, tzinfo=UTC)


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
                ),
                requires_human_approval=True,
                summary="签收争议必须核验订单、物流和签收证明。",
                effective_at=OBSERVED_AT,
            )
        ],
    )


def _actor(role: str) -> AuthenticatedActor:
    return AuthenticatedActor(
        actor_id=f"day07-{role}",
        tenant_id=TENANT_ID,
        roles=frozenset({Role(role)}),
    )


def _prepare_source(source, case) -> float:
    setup = case.get("setup")
    if setup == "dependency_failure":
        source.fail_next(case["tool"])
    if setup == "timeout":
        source.set_delay(case["tool"], 0.01)
        return 0.001
    return 0.05


class _TimedOutFuture:
    def result(self, timeout=None):
        raise FutureTimeoutError()

    def cancel(self) -> bool:
        return True


class _DeterministicTimeoutPool:
    def submit(self, function, *arguments):
        return _TimedOutFuture()


def _run_current(case) -> tuple[str, int]:
    source = _source()
    timeout = _prepare_source(source, case)
    worker_pool = (
        _DeterministicTimeoutPool()
        if case.get("setup") == "timeout"
        else None
    )
    executor = ToolExecutor(
        ToolRegistry(
            build_after_sales_tools(
                source,
                timeout_seconds=timeout,
            )
        ),
        InMemoryToolCallRecorder(),
        worker_pool=worker_pool,
    )
    try:
        observation = executor.execute(
            ToolCallRequest(
                tool_name=case["tool"],
                tool_version=case["version"],
                arguments=case["arguments"],
                context=ToolExecutionContext(
                    actor=_actor(case["role"]),
                    request_id=f"request-{case['id']}",
                    trace_id=f"trace-{case['id']}",
                    agent_run_id=f"run-{case['id']}",
                    agent_step_id="step-1",
                ),
            )
        )
    finally:
        executor.close()
    outcome = (
        "succeeded"
        if observation.succeeded
        else observation.error.kind.value
    )
    return outcome, len(source.calls)


def _run_untyped_baseline(case) -> tuple[str, int]:
    source = _source()
    _prepare_source(source, case)
    before = len(source.calls)
    try:
        tool = case["tool"]
        arguments = case["arguments"]
        if case["version"] != "1.0.0":
            raise LookupError("unknown version")
        if tool == "order_lookup":
            source.get_order(TENANT_ID, arguments["order_id"])
        elif tool == "logistics_lookup":
            source.get_logistics(TENANT_ID, arguments["order_id"])
        elif tool == "policy_lookup":
            source.get_policy(
                TicketCategory(arguments["category"]),
                arguments.get("region", "CN"),
                tenant_id=TENANT_ID,
            )
        else:
            raise LookupError("unknown tool")
    except Exception:
        outcome = "generic_error"
    else:
        outcome = "succeeded"
    return outcome, len(source.calls) - before


def run(dataset_path: Path) -> dict[str, object]:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    cases = dataset["cases"]
    baseline_exact = 0
    baseline_unauthorized_executed = 0
    baseline_invalid_arguments_reached_handler = 0
    baseline_timeouts_missed = 0
    current_exact = 0
    current_unauthorized_executed = 0
    current_invalid_arguments_reached_handler = 0
    current_timeout_detected = 0
    current_failure_kinds: set[str] = set()

    for case in cases:
        baseline_outcome, baseline_calls = _run_untyped_baseline(case)
        current_outcome, current_calls = _run_current(case)
        expected = case["expected"]
        baseline_exact += baseline_outcome == expected
        current_exact += current_outcome == expected
        if expected != "succeeded":
            current_failure_kinds.add(current_outcome)
        if expected == "authorization_error":
            baseline_unauthorized_executed += baseline_calls > 0
            current_unauthorized_executed += current_calls > 0
        if expected == "argument_error":
            baseline_invalid_arguments_reached_handler += baseline_calls > 0
            current_invalid_arguments_reached_handler += current_calls > 0
        if expected == "timeout_error":
            baseline_timeouts_missed += baseline_outcome == "succeeded"
            current_timeout_detected += current_outcome == expected

    return {
        "report_id": "day07-tool-contracts-v1",
        "dataset": {
            "dataset_id": dataset["dataset_id"],
            "description": dataset["description"],
            "synthetic": dataset["synthetic"],
            "case_count": len(cases),
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "provider": "InMemoryAfterSalesDataSource",
            "paid_api_calls": 0,
        },
        "untyped_generic_dispatch_baseline": {
            "exact_outcome_classifications": baseline_exact,
            "unauthorized_cases_reaching_handler": (
                baseline_unauthorized_executed
            ),
            "invalid_argument_cases_reaching_handler": (
                baseline_invalid_arguments_reached_handler
            ),
            "timeout_cases_not_detected": baseline_timeouts_missed,
            "failure_vocabulary": ["generic_error"],
        },
        "resolveflow_tool_executor": {
            "exact_outcome_classifications": current_exact,
            "unauthorized_cases_reaching_handler": (
                current_unauthorized_executed
            ),
            "invalid_argument_cases_reaching_handler": (
                current_invalid_arguments_reached_handler
            ),
            "timeout_cases_detected": current_timeout_detected,
            "failure_vocabulary": sorted(current_failure_kinds),
        },
        "interpretation_limits": [
            "All calls, data, failures, and timing are synthetic.",
            "This evaluates the tool execution boundary, not an LLM's tool-selection accuracy.",
            "The baseline is an intentionally untyped generic dispatcher without permission or timeout gates.",
            "No production latency, task success, or business savings are claimed.",
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
