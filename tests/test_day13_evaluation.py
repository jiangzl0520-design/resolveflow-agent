from pathlib import Path

from evaluation.run_day13_hybrid_retrieval import run


def test_day13_report_compares_channels_and_security_filters() -> None:
    root = Path(__file__).resolve().parents[1]
    report = run(
        root
        / "evaluation"
        / "datasets"
        / "day13_hybrid_retrieval_v1.json"
    )

    assert report["dataset"]["relevance_case_count"] == 60
    assert report["dataset"]["security_case_count"] == 30
    modes = report["retrieval_modes"]
    assert modes["keyword"]["recall_at_5_percent"] == 66.67
    assert modes["semantic"]["recall_at_5_percent"] == 66.67
    assert modes["hybrid"]["recall_at_5_percent"] == 100.0
    assert modes["hybrid"]["mrr"] == 1.0
    assert report["security_filters"]["checks"] == 90
    assert report["security_filters"]["unauthorized_hits"] == 0
    assert (
        report["measured_value"][
            "hybrid_recall_lift_over_best_single_channel_points"
        ]
        == 33.33
    )
