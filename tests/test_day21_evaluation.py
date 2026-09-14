from copy import deepcopy
import json

import pytest

from evaluation.day21_trajectory import (
    ResolveFlowAgentSubject,
    TrajectoryRuleEvaluator,
)
from evaluation.harness import EvalHarness
from evaluation.run_day21_trajectory import (
    DEFAULT_CONFIG,
    DEFAULT_DATASET,
    main,
    run_evaluation,
)


def _dataset():
    return EvalHarness().load_dataset(DEFAULT_DATASET)


def _case(case_id: str):
    return next(case for case in _dataset().cases if case.case_id == case_id)


def test_day21_dataset_covers_categories_and_six_failure_domains() -> None:
    dataset = _dataset()
    config = EvalHarness().load_config(DEFAULT_CONFIG)

    assert len(dataset.cases) == 18
    assert {case.category.value for case in dataset.cases} == {
        "normal",
        "boundary",
        "failure",
        "adversarial",
    }
    assert {
        case.expected["expected_failure_domain"]
        for case in dataset.cases
        if case.category.value == "failure"
    } == {"planner", "retriever", "tool", "context", "policy", "model"}
    assert config.evaluator_name == "trajectory_rule_evaluator"


@pytest.mark.parametrize(
    ("dimension", "mutate"),
    [
        (
            "task_outcome",
            lambda actual: actual.update(final_outcome="human_review_required"),
        ),
        (
            "tool_selection",
            lambda actual: actual["attempted_tools"].append(
                {
                    "name": "invented_tool",
                    "arguments": {},
                    "executed": True,
                    "status": "succeeded",
                }
            ),
        ),
        (
            "tool_arguments",
            lambda actual: actual["attempted_tools"][0]["arguments"].update(
                order_id="wrong-order"
            ),
        ),
        (
            "step_efficiency",
            lambda actual: actual.update(step_count=99),
        ),
        (
            "policy_compliance",
            lambda actual: actual["policy_violations"].append(
                "unapproved_refund_attempt"
            ),
        ),
        (
            "failure_attribution",
            lambda actual: actual.update(failure_domain="tool"),
        ),
    ],
)
def test_each_trajectory_dimension_can_independently_fail(
    dimension,
    mutate,
) -> None:
    case = _case("normal_in_transit_safe_path")
    actual = ResolveFlowAgentSubject().execute(case, seed=20260805)
    changed = deepcopy(actual)
    mutate(changed)

    assessment = TrajectoryRuleEvaluator().assess(case, changed)

    assert assessment["overall_passed"] is False
    assert assessment["dimensions"][dimension]["passed"] is False


def test_lucky_final_answer_with_dangerous_path_is_rejected() -> None:
    report = run_evaluation()
    baseline = report["trajectory_summary"]["baseline"]

    assert report["baseline"]["passed"] == 2
    assert report["candidate"]["passed"] == 18
    assert len(baseline["lucky_final_but_unsafe_cases"]) == 16
    assert baseline["dimensions"]["task_outcome"]["accuracy"] == 1.0
    assert report["comparison"]["quality_gate_passed"] is True


def test_candidate_uses_real_runner_and_typed_failure_fixtures() -> None:
    report = run_evaluation()
    by_id = {item["case_id"]: item for item in report["cases"]}

    assert (
        by_id["normal_in_transit_safe_path"]["candidate"]["actual"]
        ["execution_mode"]
        == "agent_runner"
    )
    assert (
        by_id["failure_model_attribution"]["candidate"]["actual"]
        ["execution_mode"]
        == "typed_failure_fixture"
    )


def test_transient_retry_and_refunded_terminal_boundary() -> None:
    report = run_evaluation()
    by_id = {item["case_id"]: item for item in report["cases"]}
    retry = by_id["boundary_transient_tool_retry"]["candidate"]["actual"]
    refunded = by_id[
        "boundary_refunded_prevents_duplicate_refund"
    ]["candidate"]["actual"]

    logistics_attempts = [
        item
        for item in retry["attempted_tools"]
        if item["name"] == "logistics_lookup"
    ]
    assert [item["status"] for item in logistics_attempts] == [
        "failed",
        "succeeded",
    ]
    assert logistics_attempts[0]["error_kind"] == "dependency_error"
    assert [item["name"] for item in refunded["attempted_tools"]] == [
        "order_lookup"
    ]


def test_day21_report_is_reproducible_and_contains_no_sensitive_payload() -> None:
    first = run_evaluation()
    second = run_evaluation()

    assert first == second
    serialized = json.dumps(first, ensure_ascii=False)
    assert "customer@example.com" not in serialized
    assert "TopSecret" not in serialized


def test_day21_cli_writes_report_and_uses_stable_exit_codes(tmp_path) -> None:
    output = tmp_path / "day21-report.json"

    assert main(["--output", str(output)]) == 0
    assert output.exists()
    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert persisted["candidate"]["accuracy"] == 1.0
    assert main(
        ["--minimum-accuracy", "1.1", "--output", str(output)]
    ) == 2
