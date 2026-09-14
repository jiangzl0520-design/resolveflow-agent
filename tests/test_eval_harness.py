import json

import pytest

from evaluation.day20_subjects import KeywordBaseline, TrustedFactCandidate
from evaluation.harness import EvalHarness, RunConfig, exit_code_for
from evaluation.harness.core import DatasetValidationError
from evaluation.harness.models import GoldenCase
from evaluation.run_day20_harness import DEFAULT_CONFIG, DEFAULT_DATASET


def _config(**changes) -> RunConfig:
    values = {
        "baseline_subject": "baseline@1",
        "candidate_subject": "candidate@1",
        "minimum_candidate_accuracy": 0.95,
        "maximum_regressions": 0,
    }
    values.update(changes)
    return RunConfig(**values)


def test_dataset_has_all_four_categories_and_unique_ids() -> None:
    dataset = EvalHarness().load_dataset(DEFAULT_DATASET)

    assert len(dataset.cases) == 20
    assert {case.category.value for case in dataset.cases} == {
        "normal", "boundary", "failure", "adversarial"
    }
    assert len({case.case_id for case in dataset.cases}) == 20
    config = EvalHarness().load_config(DEFAULT_CONFIG)
    assert config.harness_version == "1.0.0"
    assert config.minimum_candidate_accuracy == 0.95


def test_invalid_dataset_is_rejected_before_subject_execution(tmp_path) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(
        json.dumps({
            "schema_version": "1.0", "dataset_id": "bad_dataset",
            "dataset_version": "1.0.0", "task_type": "demo_task",
            "description": "Missing required categories on purpose.",
            "synthetic": True,
            "cases": [
                {"case_id": f"normal_{index}", "category": "normal",
                 "description": "A deliberately incomplete dataset case.",
                 "input": {}, "expected": {}}
                for index in range(4)
            ],
        }),
        encoding="utf-8",
    )

    with pytest.raises(DatasetValidationError):
        EvalHarness().load_dataset(path)


def test_dataset_with_sensitive_data_is_rejected(tmp_path) -> None:
    document = json.loads(DEFAULT_DATASET.read_text(encoding="utf-8"))
    document["cases"][0]["input"]["user_text"] = "Email customer@example.com"
    path = tmp_path / "sensitive.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(DatasetValidationError, match="de-identified"):
        EvalHarness().load_dataset(path)


def test_same_dataset_and_config_produce_identical_report() -> None:
    harness = EvalHarness()
    dataset = harness.load_dataset(DEFAULT_DATASET)
    first = harness.run(
        dataset, _config(), baseline=KeywordBaseline(),
        candidate=TrustedFactCandidate(),
    )
    second = harness.run(
        dataset, _config(), baseline=KeywordBaseline(),
        candidate=TrustedFactCandidate(),
    )

    assert first == second
    assert first.run_id == second.run_id
    assert first.dataset_sha256 == second.dataset_sha256
    assert first.config_sha256 == second.config_sha256


def test_candidate_is_compared_with_baseline_under_same_cases() -> None:
    harness = EvalHarness()
    report = harness.run(
        harness.load_dataset(DEFAULT_DATASET),
        _config(),
        baseline=KeywordBaseline(),
        candidate=TrustedFactCandidate(),
    )

    assert report.baseline.total == report.candidate.total == 20
    assert report.candidate.passed == 20
    assert report.candidate.accuracy == 1.0
    assert report.baseline.accuracy < report.candidate.accuracy
    assert report.comparison.accuracy_lift_points > 0
    assert report.comparison.regressions == ()
    assert report.comparison.quality_gate_passed is True
    assert exit_code_for(report) == 0
    assert all(score.total == 5 for score in report.candidate.categories)


def test_custom_evaluator_can_reject_lucky_output_with_unsafe_trace() -> None:
    class TraceEvaluator:
        def evaluate(self, case, actual):
            return (
                actual.get("disposition") == case.expected["disposition"]
                and not actual.get("unsafe_trace", False)
            )

    class LuckyButUnsafe:
        def execute(self, case, *, seed):
            del seed
            return {
                "disposition": case.expected["disposition"],
                "unsafe_trace": True,
            }

    harness = EvalHarness()
    dataset = harness.load_dataset(DEFAULT_DATASET)
    report = harness.run(
        dataset,
        _config(minimum_candidate_accuracy=0),
        baseline=KeywordBaseline(),
        candidate=LuckyButUnsafe(),
        evaluator=TraceEvaluator(),
    )

    assert report.candidate.passed == 0


def test_quality_gate_returns_nonzero_for_accuracy_failure() -> None:
    harness = EvalHarness()
    dataset = harness.load_dataset(DEFAULT_DATASET)
    report = harness.run(
        dataset,
        _config(minimum_candidate_accuracy=1.0),
        baseline=TrustedFactCandidate(),
        candidate=KeywordBaseline(),
    )

    assert report.comparison.quality_gate_passed is False
    assert exit_code_for(report) == 1
    assert report.comparison.regressions


def test_subject_exception_isolated_and_message_not_persisted() -> None:
    class ExplodingSubject:
        def execute(self, case: GoldenCase, *, seed: int):
            del case, seed
            raise RuntimeError("customer@example.com password=TopSecret20")

    harness = EvalHarness()
    dataset = harness.load_dataset(DEFAULT_DATASET)
    report = harness.run(
        dataset,
        _config(minimum_candidate_accuracy=0),
        baseline=KeywordBaseline(),
        candidate=ExplodingSubject(),
    )
    serialized = report.model_dump_json()

    assert report.candidate.errors == 20
    assert all(item.candidate.error_code == "subject_unclassified_error" for item in report.cases)
    assert "customer@example.com" not in serialized
    assert "TopSecret20" not in serialized


def test_sensitive_subject_output_is_redacted_after_scoring() -> None:
    class SensitiveSubject(TrustedFactCandidate):
        def execute(self, case: GoldenCase, *, seed: int):
            result = super().execute(case, seed=seed)
            result["debug"] = "customer@example.com password=TopSecret20"
            return result

    harness = EvalHarness()
    dataset = harness.load_dataset(DEFAULT_DATASET)
    report = harness.run(
        dataset, _config(), baseline=KeywordBaseline(),
        candidate=SensitiveSubject(),
    )
    serialized = report.model_dump_json()

    assert report.candidate.accuracy == 1.0
    assert "customer@example.com" not in serialized
    assert "TopSecret20" not in serialized
    assert "[REDACTED_EMAIL]" in serialized


def test_report_persistence_contains_raw_cases_and_fingerprints(tmp_path) -> None:
    harness = EvalHarness()
    dataset = harness.load_dataset(DEFAULT_DATASET)
    report = harness.run(
        dataset, _config(), baseline=KeywordBaseline(),
        candidate=TrustedFactCandidate(),
    )
    output = tmp_path / "report.json"
    harness.write_report(report, output)
    persisted = json.loads(output.read_text(encoding="utf-8"))

    assert persisted["dataset_sha256"] == report.dataset_sha256
    assert persisted["config_sha256"] == report.config_sha256
    assert len(persisted["cases"]) == 20
    assert persisted["cases"][0]["baseline"]["actual"] is not None
    assert persisted["cases"][0]["candidate"]["actual"] is not None
