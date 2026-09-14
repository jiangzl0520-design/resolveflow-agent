from pathlib import Path

from evaluation.run_day09_checkpoint_recovery import run


def test_day09_checkpoint_report_has_reproducible_deltas() -> None:
    root = Path(__file__).resolve().parents[1]
    report = run(
        root
        / "evaluation"
        / "datasets"
        / "day09_checkpoint_recovery_v1.json"
    )

    assert report["dataset"]["case_count"] == 30
    baseline = report["restart_from_scratch_baseline"]
    durable = report["resolveflow_langgraph_checkpoint"]
    delta = report["measured_delta"]
    assert baseline["correct_outcomes"] == 30
    assert durable["correct_outcomes"] == 30
    assert baseline["exact_checkpoint_resumes"] == 0
    assert durable["exact_checkpoint_resumes"] == 30
    assert baseline["duplicate_pre_restart_tool_calls"] == 30
    assert durable["duplicate_pre_restart_tool_calls"] == 0
    assert baseline["tool_calls"] == 90
    assert durable["tool_calls"] == 60
    assert delta["tool_calls_avoided"] == 30
    assert delta["tool_call_reduction_percent"] == 33.33
