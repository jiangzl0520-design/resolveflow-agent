from pathlib import Path

from evaluation.run_day06_structured_output import run

ROOT = Path(__file__).resolve().parents[1]


def test_day06_structured_output_report_is_reproducible() -> None:
    report = run(
        ROOT
        / "evaluation"
        / "datasets"
        / "day06_structured_output_v1.json"
    )
    baseline = report["baseline_json_only_no_gateway"]
    gateway = report["resolveflow_model_gateway"]

    assert baseline["total_cases"] == gateway["total_cases"] == 30
    assert baseline["safe_typed_decisions"] == 15
    assert baseline["invalid_outputs_entering_business_logic"] == 10
    assert baseline["transient_failures_recovered"] == 0
    assert gateway["safe_typed_decisions"] == 30
    assert gateway["model_decisions"] == 20
    assert gateway["conservative_fallback_decisions"] == 10
    assert gateway["invalid_outputs_entering_business_logic"] == 0
    assert gateway["transient_failures_recovered"] == 5
    assert gateway["provider_attempts"] == 35
