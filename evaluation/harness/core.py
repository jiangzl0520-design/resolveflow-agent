from collections.abc import Callable
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from app.security.content import UntrustedContentGuard

from evaluation.harness.models import (
    CaseCategory,
    CaseComparison,
    CategoryScore,
    EvalComparison,
    EvalReport,
    GoldenCase,
    GoldenDataset,
    RunConfig,
    SubjectCaseResult,
    SubjectSummary,
)


class EvaluationSubject(Protocol):
    def execute(self, case: GoldenCase, *, seed: int) -> dict[str, Any]: ...


class CaseEvaluator(Protocol):
    def evaluate(
        self,
        case: GoldenCase,
        actual: dict[str, Any],
    ) -> bool: ...


class ExpectedFieldEvaluator:
    def evaluate(
        self,
        case: GoldenCase,
        actual: dict[str, Any],
    ) -> bool:
        return _expected_fields_match(case.expected, actual)


class DatasetValidationError(ValueError):
    pass


class RunConfigurationError(ValueError):
    pass


class EvalHarness:
    """Deterministic, case-isolated offline evaluation runner."""

    def load_dataset(self, path: Path) -> GoldenDataset:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            protected = UntrustedContentGuard().protect(document)
            if protected.sensitive_data_detected:
                raise DatasetValidationError(
                    "Golden Dataset must be de-identified before approval."
                )
            return GoldenDataset.model_validate(document)
        except DatasetValidationError:
            raise
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise DatasetValidationError(
                "Golden Dataset failed schema validation."
            ) from exc

    def load_config(self, path: Path) -> RunConfig:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            return RunConfig.model_validate(document)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise RunConfigurationError(
                "Evaluation run configuration failed schema validation."
            ) from exc

    def run(
        self,
        dataset: GoldenDataset,
        config: RunConfig,
        *,
        baseline: EvaluationSubject,
        candidate: EvaluationSubject,
        evaluator: CaseEvaluator | None = None,
    ) -> EvalReport:
        resolved_evaluator = evaluator or ExpectedFieldEvaluator()
        dataset_sha = _fingerprint(dataset.model_dump(mode="json"))
        config_sha = _fingerprint(config.model_dump(mode="json"))
        comparisons = tuple(
            self._run_case(
                case,
                seed=config.seed,
                baseline=baseline,
                candidate=candidate,
                evaluator=resolved_evaluator,
            )
            for case in dataset.cases
        )
        baseline_summary = _summarize(
            comparisons,
            lambda comparison: comparison.baseline,
        )
        candidate_summary = _summarize(
            comparisons,
            lambda comparison: comparison.candidate,
        )
        improvements = tuple(
            item.case_id
            for item in comparisons
            if not item.baseline.passed and item.candidate.passed
        )
        regressions = tuple(
            item.case_id
            for item in comparisons
            if item.baseline.passed and not item.candidate.passed
        )
        gate_passed = (
            candidate_summary.accuracy >= config.minimum_candidate_accuracy
            and len(regressions) <= config.maximum_regressions
        )
        run_id = sha256(
            f"{dataset_sha}:{config_sha}".encode("utf-8")
        ).hexdigest()[:24]
        return EvalReport(
            run_id=run_id,
            dataset_id=dataset.dataset_id,
            dataset_version=dataset.dataset_version,
            dataset_sha256=dataset_sha,
            synthetic=dataset.synthetic,
            task_type=dataset.task_type,
            config_sha256=config_sha,
            config=config,
            baseline=baseline_summary,
            candidate=candidate_summary,
            comparison=EvalComparison(
                accuracy_lift_points=round(
                    (candidate_summary.accuracy - baseline_summary.accuracy)
                    * 100,
                    2,
                ),
                improvements=improvements,
                regressions=regressions,
                quality_gate_passed=gate_passed,
            ),
            cases=comparisons,
        )

    def write_report(self, report: EvalReport, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                report.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _run_case(
        self,
        case: GoldenCase,
        *,
        seed: int,
        baseline: EvaluationSubject,
        candidate: EvaluationSubject,
        evaluator: CaseEvaluator,
    ) -> CaseComparison:
        return CaseComparison(
            case_id=case.case_id,
            category=case.category,
            expected=case.expected,
            baseline=_execute_subject(
                baseline, case, seed=seed, evaluator=evaluator
            ),
            candidate=_execute_subject(
                candidate, case, seed=seed, evaluator=evaluator
            ),
        )


def exit_code_for(report: EvalReport) -> int:
    return 0 if report.comparison.quality_gate_passed else 1


def _execute_subject(
    subject: EvaluationSubject,
    case: GoldenCase,
    *,
    seed: int,
    evaluator: CaseEvaluator,
) -> SubjectCaseResult:
    try:
        actual = subject.execute(case, seed=seed)
        if not isinstance(actual, dict):
            return SubjectCaseResult(
                actual=None,
                passed=False,
                error_code="subject_output_not_object",
            )
        passed = evaluator.evaluate(case, actual)
        protected = UntrustedContentGuard().protect(actual).value
        try:
            json.dumps(protected, ensure_ascii=False)
        except (TypeError, ValueError):
            return SubjectCaseResult(
                actual=None,
                passed=False,
                error_code="subject_output_not_json",
            )
        return SubjectCaseResult(actual=protected, passed=passed)
    except Exception:
        return SubjectCaseResult(
            actual=None,
            passed=False,
            error_code="subject_unclassified_error",
        )


def _expected_fields_match(
    expected: dict[str, Any],
    actual: dict[str, Any],
) -> bool:
    return all(
        key in actual and actual[key] == value
        for key, value in expected.items()
    )


def _summarize(
    comparisons: tuple[CaseComparison, ...],
    selector: Callable[[CaseComparison], SubjectCaseResult],
) -> SubjectSummary:
    categories: list[CategoryScore] = []
    for category in CaseCategory:
        selected = [item for item in comparisons if item.category is category]
        passed = sum(selector(item).passed for item in selected)
        total = len(selected)
        categories.append(
            CategoryScore(
                category=category,
                passed=passed,
                total=total,
                accuracy=round(passed / total, 4) if total else 0.0,
            )
        )
    total = len(comparisons)
    passed = sum(selector(item).passed for item in comparisons)
    errors = sum(selector(item).error_code is not None for item in comparisons)
    return SubjectSummary(
        passed=passed,
        total=total,
        accuracy=round(passed / total, 4) if total else 0.0,
        errors=errors,
        categories=tuple(categories),
    )


def _fingerprint(document: dict[str, Any]) -> str:
    canonical = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(canonical).hexdigest()
