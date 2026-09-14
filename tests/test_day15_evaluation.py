from pathlib import Path

from evaluation.run_day15_context_builder import run


def test_day15_report_compares_context_builder_with_naive_baseline() -> None:
    root = Path(__file__).resolve().parents[1]
    report = run(
        root
        / "evaluation"
        / "datasets"
        / "day15_context_builder_v1.json"
    )

    assert report["dataset"]["case_count"] == 60
    assert report["runtime"]["paid_model_calls"] == 0
    assert report["context_builder"][
        "critical_fragment_retention_percent"
    ] == 100.0
    assert report["context_builder"]["stale_fragment_selections"] == 0
    assert report["context_builder"][
        "dynamic_tool_precision_percent"
    ] == 100.0
    assert report["measured_value"]["critical_retention_lift_points"] > 0
    assert report["measured_value"]["stale_selections_removed"] == 40
    assert report["measured_value"]["tool_precision_lift_points"] > 0
