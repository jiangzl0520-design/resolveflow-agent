from pathlib import Path

from evaluation.run_day16_long_term_memory import run


def test_day16_report_compares_policy_memory_with_save_everything_baseline() -> None:
    root = Path(__file__).resolve().parents[1]
    report = run(
        root
        / "evaluation"
        / "datasets"
        / "day16_long_term_memory_v1.json"
    )

    assert report["dataset"]["case_count"] == 80
    assert report["runtime"]["paid_model_calls"] == 0
    assert report["runtime"]["embedding_calls"] == 0
    assert report["policy_controlled_memory"][
        "correct_handling_percent"
    ] == 100.0
    assert report["policy_controlled_memory"]["unsafe_writes"] == 0
    assert report["policy_controlled_memory"]["unsafe_recalls"] == 0
    assert report["measured_value"]["unsafe_writes_blocked"] == 40
    assert report["measured_value"]["unsafe_recalls_blocked"] == 15
    assert report["measured_value"]["correct_handling_lift_points"] > 0
    assert report["measured_value"]["memory_payload_reduction_percent"] > 90
