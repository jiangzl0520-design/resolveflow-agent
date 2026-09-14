from pathlib import Path

from evaluation.run_day14_grounded_answer import run


def test_day14_report_measures_grounding_gates_against_baseline() -> None:
    root = Path(__file__).resolve().parents[1]
    report = run(
        root
        / "evaluation"
        / "datasets"
        / "day14_grounded_answer_v1.json"
    )

    assert report["dataset"]["case_count"] == 60
    assert report["baseline"]["correct_handling_percent"] == 33.33
    assert report["baseline"]["citation_precision_percent"] == 33.33
    assert report["baseline"]["expired_policy_citations"] == 10
    assert report["verified_pipeline"]["correct_handling_percent"] == 100.0
    assert report["verified_pipeline"]["citation_precision_percent"] == 100.0
    assert report["verified_pipeline"]["expired_policy_citations"] == 0
    assert report["verified_pipeline"]["forged_citations_blocked"] == 10
    assert report["verified_pipeline"]["runs_recorded_percent"] == 100.0
    assert report["verified_pipeline"]["model_calls_recorded"] == 70
    assert report["measured_value"]["correct_handling_lift_points"] == 66.67
    assert report["measured_value"]["citation_precision_lift_points"] == 66.67
