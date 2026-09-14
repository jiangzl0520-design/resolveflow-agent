from pathlib import Path

from evaluation.run_day10_refund_safety import run


def test_day10_safety_report_has_reproducible_value_metrics() -> None:
    root = Path(__file__).resolve().parents[1]
    report = run(
        root
        / "evaluation"
        / "datasets"
        / "day10_refund_safety_v1.json"
    )

    assert report["dataset"]["case_count"] == 55
    baseline = report["unsafe_direct_execution_baseline"]
    current = report["resolveflow_policy_approval"]
    idempotency = report["idempotency_replay"]
    delta = report["measured_delta"]
    assert baseline["write_executions"] == 55
    assert baseline["unapproved_or_invalid_write_executions"] == 45
    assert current["correct_safety_outcomes"] == 55
    assert current["write_executions"] == 10
    assert current["unapproved_or_invalid_write_executions"] == 0
    assert current["valid_approved_refunds_executed_and_verified"] == 10
    assert idempotency["trials"] == 20
    assert idempotency["duplicate_side_effects"] == 0
    assert delta["unsafe_writes_avoided"] == 45
    assert delta["unsafe_write_reduction_percent"] == 100.0
