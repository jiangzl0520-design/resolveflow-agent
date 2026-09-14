from pathlib import Path

from evaluation.run_day17_context_security import run


def test_day17_report_compares_prompt_only_with_layered_security() -> None:
    root = Path(__file__).resolve().parents[1]
    report = run(
        root
        / "evaluation"
        / "datasets"
        / "day17_context_security_v1.json"
    )

    assert report["dataset"]["case_count"] == 100
    assert report["dataset"]["adversarial_case_count"] == 80
    assert report["dataset"]["benign_case_count"] == 20
    assert report["runtime"]["paid_model_calls"] == 0
    assert report["runtime"]["embedding_calls"] == 0
    assert report["layered_security"]["correct_handling_percent"] == 100.0
    assert report["layered_security"][
        "malicious_fragments_reaching_model"
    ] == 0
    assert report["layered_security"]["sensitive_exposures"] == 0
    assert report["layered_security"]["unsafe_actions_allowed"] == 0
    assert report["layered_security"]["false_positive_blocks"] == 0
    assert report["measured_value"]["additional_attacks_contained"] == 80
    assert report["measured_value"][
        "malicious_model_exposures_prevented"
    ] == 40
    assert report["measured_value"]["sensitive_exposures_prevented"] == 20
    assert report["measured_value"]["unsafe_actions_prevented"] == 20
