from argparse import ArgumentParser
import importlib.metadata
import json
from pathlib import Path
import platform
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver

from app.agent.runner import AgentRunner
from app.agent.workflow import DurableAgentWorkflow
from app.tools.contracts import ToolCallRequest, ToolExecutionContext
from app.tools.executor import InMemoryToolCallRecorder, ToolExecutor
from app.tools.registry import ToolRegistry
from app.tools.after_sales import build_after_sales_tools
try:
    from evaluation.run_day08_agent_loop import (
        ObservationDrivenPlanner,
        _actor,
        _command,
        _source,
    )
except ModuleNotFoundError:
    from run_day08_agent_loop import (
        ObservationDrivenPlanner,
        _actor,
        _command,
        _source,
    )

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = (
    ROOT
    / "evaluation"
    / "datasets"
    / "day09_checkpoint_recovery_v1.json"
)


def _first_order_lookup(scenario: str, case_id: str) -> int:
    source = _source(scenario)
    registry = ToolRegistry(build_after_sales_tools(source))
    recorder = InMemoryToolCallRecorder()
    executor = ToolExecutor(registry, recorder)
    try:
        executor.execute(
            ToolCallRequest(
                tool_name="order_lookup",
                tool_version="1.0.0",
                arguments={"order_id": "10086"},
                context=ToolExecutionContext(
                    actor=_actor(),
                    request_id=f"baseline-before-{case_id}",
                    trace_id=f"baseline-before-{case_id}",
                    agent_run_id=f"baseline-before-{case_id}",
                    agent_step_id="step-1",
                ),
            )
        )
    finally:
        executor.close()
    return len(source.calls)


def _baseline_restart(
    scenario: str,
    case_id: str,
) -> tuple[str | None, int, int]:
    calls_before_restart = _first_order_lookup(scenario, case_id)
    restarted_source = _source(scenario)
    registry = ToolRegistry(build_after_sales_tools(restarted_source))
    executor = ToolExecutor(registry, InMemoryToolCallRecorder())
    try:
        state = AgentRunner(
            ObservationDrivenPlanner(),
            registry,
            executor,
        ).run(_command(f"baseline-restart-{case_id}"))
    finally:
        executor.close()
    restarted_calls = [item[0] for item in restarted_source.calls]
    return (
        state.final_outcome.value
        if state.final_outcome is not None
        else None,
        calls_before_restart + len(restarted_calls),
        int("order_lookup" in restarted_calls),
    )


def _durable_restart(
    scenario: str,
    case_id: str,
) -> tuple[str | None, int, int, int]:
    saver = InMemorySaver()
    first_source = _source(scenario)
    first_registry = ToolRegistry(
        build_after_sales_tools(first_source)
    )
    first_executor = ToolExecutor(
        first_registry,
        InMemoryToolCallRecorder(),
    )
    run_id = uuid4()
    try:
        paused = DurableAgentWorkflow(
            ObservationDrivenPlanner(),
            first_registry,
            first_executor,
            saver,
        ).start(
            _command(f"durable-before-{case_id}"),
            run_id=run_id,
            pause_after_step=1,
        )
    finally:
        first_executor.close()
    if not paused.paused:
        raise AssertionError("Expected a checkpoint pause.")

    restarted_source = _source(scenario)
    restarted_registry = ToolRegistry(
        build_after_sales_tools(restarted_source)
    )
    restarted_executor = ToolExecutor(
        restarted_registry,
        InMemoryToolCallRecorder(),
    )
    restarted_workflow = DurableAgentWorkflow(
        ObservationDrivenPlanner(),
        restarted_registry,
        restarted_executor,
        saver,
    )
    try:
        completed = restarted_workflow.resume(run_id)
        checkpoint_count = restarted_workflow.checkpoint_count(run_id)
    finally:
        restarted_executor.close()
    restarted_calls = [item[0] for item in restarted_source.calls]
    return (
        completed.state.final_outcome.value
        if completed.state.final_outcome is not None
        else None,
        len(first_source.calls) + len(restarted_calls),
        int("order_lookup" in restarted_calls),
        checkpoint_count,
    )


def run(dataset_path: Path) -> dict[str, object]:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    baseline_correct = 0
    baseline_calls = 0
    baseline_duplicate_calls = 0
    durable_correct = 0
    durable_calls = 0
    durable_duplicate_calls = 0
    durable_checkpoint_counts: list[int] = []
    case_count = 0

    for scenario, count in dataset["scenario_counts"].items():
        for index in range(count):
            case_count += 1
            case_id = f"{scenario}-{index:02d}"
            expected = dataset["expected_outcomes"][scenario]
            baseline = _baseline_restart(scenario, case_id)
            durable = _durable_restart(scenario, case_id)
            baseline_correct += baseline[0] == expected
            baseline_calls += baseline[1]
            baseline_duplicate_calls += baseline[2]
            durable_correct += durable[0] == expected
            durable_calls += durable[1]
            durable_duplicate_calls += durable[2]
            durable_checkpoint_counts.append(durable[3])

    avoided_calls = baseline_calls - durable_calls
    return {
        "report_id": "day09-checkpoint-recovery-v1",
        "dataset": {**dataset, "case_count": case_count},
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "langgraph": importlib.metadata.version("langgraph"),
            "checkpointer": "InMemorySaver for deterministic evaluation",
            "planner": "deterministic_observation_driven",
            "paid_api_calls": 0,
        },
        "restart_from_scratch_baseline": {
            "correct_outcomes": baseline_correct,
            "exact_checkpoint_resumes": 0,
            "tool_calls": baseline_calls,
            "duplicate_pre_restart_tool_calls": (
                baseline_duplicate_calls
            ),
        },
        "resolveflow_langgraph_checkpoint": {
            "correct_outcomes": durable_correct,
            "exact_checkpoint_resumes": case_count,
            "tool_calls": durable_calls,
            "duplicate_pre_restart_tool_calls": (
                durable_duplicate_calls
            ),
            "minimum_checkpoints_per_run": min(
                durable_checkpoint_counts
            ),
        },
        "measured_delta": {
            "tool_calls_avoided": avoided_calls,
            "tool_call_reduction_percent": round(
                avoided_calls / baseline_calls * 100,
                2,
            ),
            "duplicate_checkpointed_call_reduction": (
                baseline_duplicate_calls - durable_duplicate_calls
            ),
        },
        "durability_evidence": {
            "postgres_runtime_recreation_test": (
                "tests/test_agent_workflow.py::"
                "test_postgres_checkpoint_survives_runtime_recreation"
            ),
            "postgres_test_is_separate_from_this_offline_report": True,
        },
        "interpretation_limits": [
            "All business facts, interruptions, and planner decisions are synthetic.",
            "The offline comparison uses InMemorySaver so it is deterministic and does not claim storage durability.",
            "A separate PostgreSQL integration test proves runtime recreation against a durable checkpointer.",
            "This measures avoided repeated tool execution after a saved checkpoint, not production revenue or latency.",
            "A side effect that succeeds before its checkpoint is committed still requires an idempotency key; Day10 write tools must enforce that separately.",
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
