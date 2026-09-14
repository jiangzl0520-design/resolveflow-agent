from evaluation.run_day18_trace_diagnosis import run_evaluation


def test_day18_trace_diagnosis_evaluation_is_reproducible() -> None:
    first = run_evaluation()
    second = run_evaluation()

    assert first == second
    assert first["synthetic"] is True
    assert first["sample"] == {
        "total_cases": 70,
        "failure_cases": 60,
        "benign_cases": 10,
        "failure_domains": [
            "database",
            "model",
            "policy",
            "retrieval",
            "security",
            "tool",
        ],
    }
    baseline = first["correlation_id_only_baseline"]
    traced = first["opentelemetry_component_trace"]
    assert baseline["root_cause_accuracy_percent"] == 14.29
    assert baseline["sensitive_exposures"] == 60
    assert traced["root_cause_accuracy_percent"] == 100.0
    assert traced["unclassified_failures"] == 0
    assert traced["sensitive_exposures"] == 0
    assert traced["false_failure_diagnoses"] == 0
    assert first["measured_value"] == {
        "root_cause_accuracy_lift_points": 85.71,
        "additional_failures_localized": 60,
        "sensitive_exposures_prevented": 60,
    }
