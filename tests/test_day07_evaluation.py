from evaluation.run_day07_tool_contracts import (
    DEFAULT_DATASET,
    run,
)


def test_day07_tool_contract_evaluation_is_reproducible() -> None:
    report = run(DEFAULT_DATASET)

    assert report["dataset"]["synthetic"] is True
    assert report["dataset"]["case_count"] == 30
    baseline = report["untyped_generic_dispatch_baseline"]
    current = report["resolveflow_tool_executor"]
    assert baseline["exact_outcome_classifications"] == 10
    assert current["exact_outcome_classifications"] == 30
    assert baseline["unauthorized_cases_reaching_handler"] == 3
    assert current["unauthorized_cases_reaching_handler"] == 0
    assert baseline["invalid_argument_cases_reaching_handler"] >= 1
    assert current["invalid_argument_cases_reaching_handler"] == 0
    assert baseline["timeout_cases_not_detected"] == 3
    assert current["timeout_cases_detected"] == 3
    assert len(current["failure_vocabulary"]) == 6
