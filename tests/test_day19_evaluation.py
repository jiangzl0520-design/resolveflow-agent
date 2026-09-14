from evaluation.run_day19_observability import run_evaluation


def test_day19_evaluation_is_reproducible_and_attributes_regression() -> None:
    first = run_evaluation(write_report=False)
    second = run_evaluation(write_report=False)

    assert first == second
    assert first["sample_size"] == 200
    assert first["baseline"]["correct_cause"] is False
    assert first["final"]["correct_cause"] is True
    assert first["final"]["success_rate_change_percentage_points"] == -25.0
    assert first["final"]["top_failure_domain"] == "tool"
    assert first["final"]["attributed_failure_share"] == 0.6667
    assert first["production_claim"] is False
