import json
from datetime import UTC, datetime

import pytest

from app.evaluation.judge import ExplanationJudge
from app.llm.contracts import RawProviderResponse
from app.llm.errors import ModelOutputValidationError
from app.llm.fake_provider import FakeLLMProvider
from app.llm.gateway import ModelGateway
from app.repositories.model_call_repository import InMemoryModelCallRepository
from evaluation.day22_calibration import (
    CalibrationLoadError,
    TENANT_ID,
    load_calibration_config,
    load_calibration_dataset,
    load_judge_rubric,
)
from evaluation.run_day22_judge import (
    DEFAULT_CONFIG,
    DEFAULT_DATASET,
    DEFAULT_RUBRIC,
    main,
    run_evaluation,
)


def _gateway(provider, repository=None):
    return ModelGateway(
        provider,
        repository or InMemoryModelCallRepository(),
        model="day22-test-judge",
        reasoning_effort="low",
        max_output_tokens=500,
        max_attempts=1,
        retry_base_seconds=1,
        wall_clock=lambda: datetime(2026, 8, 5, tzinfo=UTC),
        monotonic_clock=lambda: 1.0,
    )


def test_dataset_is_double_annotated_and_covers_four_categories() -> None:
    dataset = load_calibration_dataset(DEFAULT_DATASET)

    assert len(dataset.cases) == 8
    assert {case.category.value for case in dataset.cases} == {
        "normal",
        "boundary",
        "failure",
        "adversarial",
    }
    assert all(len(case.human_annotations) == 2 for case in dataset.cases)
    assert all(
        len({item.annotator_token for item in case.human_annotations}) == 2
        for case in dataset.cases
    )


def test_rubric_scores_only_semantics_and_excludes_hard_facts() -> None:
    rubric = load_judge_rubric(DEFAULT_RUBRIC)
    exclusions = " ".join(rubric.exclusions).casefold()

    assert {item.name for item in rubric.dimensions} == {
        "clarity",
        "evidence_grounding",
        "reasoning_completeness",
        "actionability",
    }
    assert "tool" in exclusions
    assert "authorization" in exclusions
    assert "policy" in exclusions
    assert "safety gate" in exclusions


def test_judge_payload_is_blind_and_uses_structured_model_gateway() -> None:
    dataset = load_calibration_dataset(DEFAULT_DATASET)
    case = dataset.cases[0]
    provider = FakeLLMProvider(
        [
            RawProviderResponse(
                output=case.judge_original.model_dump(mode="json"),
                model="day22-test-judge",
            )
        ]
    )
    repository = InMemoryModelCallRepository()
    judge = ExplanationJudge(
        _gateway(provider, repository),
        load_judge_rubric(DEFAULT_RUBRIC),
        tenant_id=TENANT_ID,
    )

    result = judge.evaluate(
        case_ref="blind-case-1",
        task=case.task,
        response_a=case.response_a,
        response_b=case.response_b,
        request_id="day22-blind-request",
        trace_id="day22-blind-trace",
    )

    payload = provider.requests[0].input_text.casefold()
    assert result.preference.value == "response_b"
    assert "baseline" not in payload
    assert "candidate" not in payload
    assert "response_a" in payload and "response_b" in payload
    assert repository.records[0].prompt_version == "1.0.0"


def test_score_preference_contradiction_is_rejected() -> None:
    dataset = load_calibration_dataset(DEFAULT_DATASET)
    invalid = dataset.cases[0].judge_original.model_dump(mode="json")
    invalid["preference"] = "response_a"
    provider = FakeLLMProvider(
        [RawProviderResponse(output=invalid, model="day22-test-judge")]
    )
    judge = ExplanationJudge(
        _gateway(provider),
        load_judge_rubric(DEFAULT_RUBRIC),
        tenant_id=TENANT_ID,
    )

    with pytest.raises(ModelOutputValidationError):
        judge.evaluate(
            case_ref="invalid-judge-case",
            task=dataset.cases[0].task,
            response_a=dataset.cases[0].response_a,
            response_b=dataset.cases[0].response_b,
            request_id="day22-invalid-request",
            trace_id="day22-invalid-trace",
        )


def test_calibration_measures_human_agreement_consistency_and_bias() -> None:
    report = run_evaluation()

    assert report["human_calibration"]["preference_agreement"] == 0.875
    assert (
        report["judge_calibration"]["judge_human_preference_agreement"]
        == 0.875
    )
    assert report["judge_calibration"]["order_consistency"] == 0.875
    assert report["judge_calibration"]["order_sensitivity"] == 0.125
    assert report["judge_calibration"]["scores_within_one_rate"] == 1.0
    assert report["judge_calibration"]["calibration_gate_passed"] is True
    assert report["semantic_quality"] == {
        "baseline_mean_score": 2.25,
        "candidate_mean_score": 3.4375,
        "candidate_lift": 1.1875,
    }


def test_semantic_score_cannot_override_failed_hard_gate() -> None:
    report = run_evaluation()
    cases = {item["case_id"]: item for item in report["case_results"]}
    control = cases["adversarial_judge_cannot_override_safety"]

    assert control["candidate_semantic_score"] == 4.0
    assert control["semantic_gate_passed"] is True
    assert control["candidate_hard_gate_passed"] is False
    assert control["release_decision"] is False
    assert (
        control["semantic_preference_could_not_override_hard_gate"] is True
    )
    assert report["combined_quality_gate"]["judge_override_attempts_blocked"] == 1
    assert report["combined_quality_gate"]["passed"] is True


def test_swapped_order_exposes_one_position_sensitive_case() -> None:
    report = run_evaluation()
    sensitive = [
        item for item in report["case_results"] if not item["order_consistent"]
    ]

    assert [item["case_id"] for item in sensitive] == [
        "adversarial_order_sensitivity_probe"
    ]
    assert sensitive[0]["judge_aggregate_preference"] == "unstable"
    assert sensitive[0]["judge_matches_human"] is False


def test_report_is_reproducible_and_records_all_gateway_calls() -> None:
    first = run_evaluation()
    second = run_evaluation()

    assert first == second
    assert first["execution"]["judge_calls"] == 16
    assert first["execution"]["calls_per_case"] == 2
    assert first["blinding"]["model_identity_removed_from_payload"] is True
    assert len(first["interpretation_limits"]) >= 5


def test_sensitive_calibration_artifact_is_rejected(tmp_path) -> None:
    document = json.loads(DEFAULT_DATASET.read_text(encoding="utf-8"))
    document["cases"][0]["response_a"] = "Contact customer@example.com now."
    path = tmp_path / "sensitive-calibration.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(CalibrationLoadError, match="de-identified"):
        load_calibration_dataset(path)


def test_day22_cli_writes_report_and_returns_stable_exit_codes(tmp_path) -> None:
    output = tmp_path / "day22-report.json"
    invalid = tmp_path / "invalid-config.json"
    invalid.write_text("{}", encoding="utf-8")

    assert main(["--output", str(output)]) == 0
    assert output.exists()
    assert main(["--config", str(invalid), "--output", str(output)]) == 2
    assert load_calibration_config(DEFAULT_CONFIG).minimum_semantic_score == 3
