from evaluation.run_day08_agent_loop import DEFAULT_DATASET, run


def test_day08_agent_loop_evaluation_is_reproducible() -> None:
    report = run(DEFAULT_DATASET)

    assert report["dataset"]["synthetic"] is True
    assert report["dataset"]["case_count"] == 30
    baseline = report["fixed_three_tool_pipeline_baseline"]
    current = report["resolveflow_minimal_agent_loop"]
    assert baseline["completed_runs"] == 13
    assert baseline["correct_outcomes"] == 13
    assert baseline["tool_calls"] == 73
    assert (
        baseline["unnecessary_calls_after_terminal_order_fact"]
        == 12
    )
    assert baseline["transient_failures_recovered"] == 0
    assert current["completed_runs"] == 30
    assert current["correct_outcomes"] == 30
    assert current["tool_calls"] == 71
    assert current["unnecessary_calls_after_terminal_order_fact"] == 0
    assert current["transient_failures_recovered"] == 5
