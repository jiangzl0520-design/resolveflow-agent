from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.evaluation.judge import (
    ExplanationJudge,
    JudgePreference,
    JudgeResult,
    JudgeRubric,
    SemanticScores,
)
from app.llm.contracts import RawProviderResponse
from app.llm.fake_provider import FakeLLMProvider
from app.llm.gateway import ModelGateway
from app.repositories.model_call_repository import InMemoryModelCallRepository
from app.security.content import UntrustedContentGuard
from evaluation.harness.models import CaseCategory


TENANT_ID = UUID("22000000-0000-0000-0000-000000000001")
SCORE_FIELDS = tuple(SemanticScores.model_fields)


class VariantPreference(StrEnum):
    BASELINE = "baseline"
    CANDIDATE = "candidate"
    TIE = "tie"
    UNSTABLE = "unstable"


class HumanAnnotation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    annotator_token: str = Field(pattern=r"^ann-[a-z0-9]{4}$")
    preference: JudgePreference


class HumanAdjudication(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    preference: JudgePreference
    response_a: SemanticScores
    response_b: SemanticScores


class CalibrationCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    category: CaseCategory
    task: str = Field(min_length=10, max_length=500)
    response_a: str = Field(min_length=10, max_length=1000)
    response_b: str = Field(min_length=10, max_length=1000)
    human_annotations: tuple[HumanAnnotation, ...] = Field(
        min_length=2,
        max_length=2,
    )
    adjudicated: HumanAdjudication
    judge_original: JudgeResult
    judge_swapped: JudgeResult
    candidate_hard_gate_passed: bool
    expected_release_decision: bool

    @model_validator(mode="after")
    def validate_annotation_pair(self) -> "CalibrationCase":
        tokens = [item.annotator_token for item in self.human_annotations]
        if len(tokens) != len(set(tokens)):
            raise ValueError("Human annotators must be independently identified.")
        return self


class CalibrationDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(pattern=r"^1\.0$")
    dataset_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    dataset_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str = Field(min_length=20, max_length=500)
    synthetic: bool
    cases: tuple[CalibrationCase, ...] = Field(min_length=8)

    @model_validator(mode="after")
    def validate_cases(self) -> "CalibrationDataset":
        identifiers = [case.case_id for case in self.cases]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Calibration case ids must be unique.")
        missing = set(CaseCategory) - {case.category for case in self.cases}
        if missing:
            raise ValueError("Calibration dataset must cover all categories.")
        return self


class CalibrationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    config_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    judge_model: str = Field(min_length=3, max_length=100)
    minimum_judge_human_preference_agreement: float = Field(ge=0, le=1)
    minimum_order_consistency: float = Field(ge=0, le=1)
    maximum_order_sensitivity: float = Field(ge=0, le=1)
    minimum_scores_within_one_rate: float = Field(ge=0, le=1)
    minimum_semantic_score: float = Field(ge=1, le=4)
    minimum_release_decision_accuracy: float = Field(ge=0, le=1)


class CalibrationLoadError(ValueError):
    pass


def load_calibration_dataset(path: Path) -> CalibrationDataset:
    return _load_model(path, CalibrationDataset)


def load_calibration_config(path: Path) -> CalibrationConfig:
    return _load_model(path, CalibrationConfig)


def load_judge_rubric(path: Path) -> JudgeRubric:
    return _load_model(path, JudgeRubric)


def run_calibration(
    dataset: CalibrationDataset,
    config: CalibrationConfig,
    rubric: JudgeRubric,
) -> dict[str, Any]:
    outcomes = []
    for case in dataset.cases:
        outcomes.extend(
            (
                _raw(case.judge_original, config.judge_model),
                _raw(case.judge_swapped, config.judge_model),
            )
        )
    provider = FakeLLMProvider(outcomes)
    repository = InMemoryModelCallRepository()
    gateway = ModelGateway(
        provider,
        repository,
        model=config.judge_model,
        reasoning_effort="low",
        max_output_tokens=500,
        max_attempts=1,
        retry_base_seconds=1,
        wall_clock=lambda: _FIXED_TIME,
        monotonic_clock=lambda: 1.0,
    )
    judge = ExplanationJudge(gateway, rubric, tenant_id=TENANT_ID)

    case_results = []
    preference_matches = 0
    order_consistent = 0
    human_agreements = 0
    score_differences: list[float] = []
    baseline_semantic_scores: list[float] = []
    candidate_semantic_scores: list[float] = []
    release_matches = 0
    overrides_blocked = 0

    for case in dataset.cases:
        original = judge.evaluate(
            case_ref=f"{case.case_id}:original",
            task=case.task,
            response_a=case.response_a,
            response_b=case.response_b,
            request_id=f"day22-{case.case_id}-original",
            trace_id=f"day22-trace-{case.case_id}",
        )
        swapped = judge.evaluate(
            case_ref=f"{case.case_id}:swapped",
            task=case.task,
            response_a=case.response_b,
            response_b=case.response_a,
            request_id=f"day22-{case.case_id}-swapped",
            trace_id=f"day22-trace-{case.case_id}",
        )

        original_preference = _normalize_preference(
            original.preference,
            swapped=False,
        )
        swapped_preference = _normalize_preference(
            swapped.preference,
            swapped=True,
        )
        consistent = original_preference is swapped_preference
        aggregate = (
            original_preference
            if consistent
            else VariantPreference.UNSTABLE
        )
        human_preference = _normalize_preference(
            case.adjudicated.preference,
            swapped=False,
        )
        human_agreement = (
            case.human_annotations[0].preference
            == case.human_annotations[1].preference
        )
        preference_match = aggregate is human_preference

        judge_scores = _average_normalized_scores(original, swapped)
        human_scores = {
            "baseline": case.adjudicated.response_a.model_dump(),
            "candidate": case.adjudicated.response_b.model_dump(),
        }
        for variant in ("baseline", "candidate"):
            for dimension in SCORE_FIELDS:
                score_differences.append(
                    abs(
                        judge_scores[variant][dimension]
                        - human_scores[variant][dimension]
                    )
                )

        candidate_semantic_score = _mean(
            judge_scores["candidate"].values()
        )
        baseline_semantic_score = _mean(
            judge_scores["baseline"].values()
        )
        baseline_semantic_scores.append(baseline_semantic_score)
        candidate_semantic_scores.append(candidate_semantic_score)
        semantic_passed = (
            candidate_semantic_score >= config.minimum_semantic_score
        )
        release_decision = (
            case.candidate_hard_gate_passed and semantic_passed
        )
        release_match = (
            release_decision is case.expected_release_decision
        )
        override_blocked = (
            not case.candidate_hard_gate_passed
            and semantic_passed
            and not release_decision
        )

        preference_matches += int(preference_match)
        order_consistent += int(consistent)
        human_agreements += int(human_agreement)
        release_matches += int(release_match)
        overrides_blocked += int(override_blocked)
        case_results.append(
            {
                "case_id": case.case_id,
                "category": case.category.value,
                "human_annotators_agreed": human_agreement,
                "human_adjudicated_preference": human_preference.value,
                "judge_original_preference": original_preference.value,
                "judge_swapped_preference": swapped_preference.value,
                "judge_aggregate_preference": aggregate.value,
                "judge_matches_human": preference_match,
                "order_consistent": consistent,
                "judge_scores": judge_scores,
                "baseline_semantic_score": round(
                    baseline_semantic_score,
                    4,
                ),
                "candidate_semantic_score": round(
                    candidate_semantic_score,
                    4,
                ),
                "candidate_hard_gate_passed": (
                    case.candidate_hard_gate_passed
                ),
                "semantic_gate_passed": semantic_passed,
                "release_decision": release_decision,
                "expected_release_decision": (
                    case.expected_release_decision
                ),
                "release_decision_correct": release_match,
                "semantic_preference_could_not_override_hard_gate": (
                    override_blocked
                ),
            }
        )

    total = len(dataset.cases)
    preference_agreement = preference_matches / total
    order_consistency = order_consistent / total
    order_sensitivity = 1 - order_consistency
    within_one_rate = sum(
        difference <= 1 for difference in score_differences
    ) / len(score_differences)
    release_accuracy = release_matches / total
    calibration_gate = (
        preference_agreement
        >= config.minimum_judge_human_preference_agreement
        and order_consistency >= config.minimum_order_consistency
        and order_sensitivity <= config.maximum_order_sensitivity
        and within_one_rate >= config.minimum_scores_within_one_rate
    )
    combined_gate = (
        calibration_gate
        and release_accuracy >= config.minimum_release_decision_accuracy
    )
    blind_payloads = all(
        "baseline" not in request.input_text.casefold()
        and "candidate" not in request.input_text.casefold()
        for request in provider.requests
    )

    return {
        "report_schema_version": "1.0",
        "report_id": "day22-judge-calibration-v1",
        "dataset": {
            "dataset_id": dataset.dataset_id,
            "dataset_version": dataset.dataset_version,
            "synthetic": dataset.synthetic,
            "case_count": total,
            "categories": _category_counts(dataset.cases),
        },
        "rubric": {
            "rubric_id": rubric.rubric_id,
            "version": rubric.version,
            "dimensions": [item.name for item in rubric.dimensions],
            "excluded_hard_checks": list(rubric.exclusions),
        },
        "config": config.model_dump(mode="json"),
        "execution": {
            "provider": "deterministic structured-output fixture",
            "model": config.judge_model,
            "judge_calls": len(repository.records),
            "calls_per_case": 2,
            "paid_api_calls": 0,
            "raw_responses_persisted": False,
        },
        "blinding": {
            "model_identity_removed_from_payload": blind_payloads,
            "responses_named_only_a_and_b": True,
            "order_swapped_for_every_case": True,
            "human_annotator_tokens_are_blind": True,
        },
        "human_calibration": {
            "double_annotated_cases": total,
            "preference_agreement": round(human_agreements / total, 4),
            "disagreements_require_adjudication": True,
        },
        "judge_calibration": {
            "judge_human_preference_agreement": round(
                preference_agreement,
                4,
            ),
            "order_consistency": round(order_consistency, 4),
            "order_sensitivity": round(order_sensitivity, 4),
            "score_mean_absolute_error": round(
                _mean(score_differences),
                4,
            ),
            "scores_within_one_rate": round(within_one_rate, 4),
            "calibration_gate_passed": calibration_gate,
        },
        "semantic_quality": {
            "baseline_mean_score": round(
                _mean(baseline_semantic_scores),
                4,
            ),
            "candidate_mean_score": round(
                _mean(candidate_semantic_scores),
                4,
            ),
            "candidate_lift": round(
                _mean(candidate_semantic_scores)
                - _mean(baseline_semantic_scores),
                4,
            ),
        },
        "combined_quality_gate": {
            "release_decision_accuracy": round(release_accuracy, 4),
            "judge_override_attempts_blocked": overrides_blocked,
            "passed": combined_gate,
            "rule": (
                "deterministic_hard_gate AND calibrated_semantic_gate"
            ),
        },
        "case_results": case_results,
        "interpretation_limits": [
            "The Judge evaluates semantic explanation quality only after deterministic safety checks.",
            "A semantic score can never override failed authorization, policy, tool, argument, or trajectory gates.",
            "Two swapped runs measure order sensitivity, not every possible Judge bias.",
            "Human labels are a small synthetic calibration fixture, not production ground truth.",
            "One aggregate Judge score is not treated as truth; dimension scores, disagreements, and hard gates remain visible.",
        ],
    }


def _load_model(path: Path, model_type):
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        protected = UntrustedContentGuard().protect(document)
        if protected.sensitive_data_detected:
            raise CalibrationLoadError(
                "Calibration artifacts must be de-identified."
            )
        return model_type.model_validate(document)
    except CalibrationLoadError:
        raise
    except Exception as exc:
        raise CalibrationLoadError(
            "Calibration artifact failed validation."
        ) from exc


def _raw(result: JudgeResult, model: str) -> RawProviderResponse:
    return RawProviderResponse(
        output=result.model_dump(mode="json"),
        model=model,
        input_tokens=180,
        output_tokens=40,
    )


def _normalize_preference(
    preference: JudgePreference,
    *,
    swapped: bool,
) -> VariantPreference:
    if preference is JudgePreference.TIE:
        return VariantPreference.TIE
    if swapped:
        return (
            VariantPreference.CANDIDATE
            if preference is JudgePreference.RESPONSE_A
            else VariantPreference.BASELINE
        )
    return (
        VariantPreference.BASELINE
        if preference is JudgePreference.RESPONSE_A
        else VariantPreference.CANDIDATE
    )


def _average_normalized_scores(
    original: JudgeResult,
    swapped: JudgeResult,
) -> dict[str, dict[str, float]]:
    original_baseline = original.response_a.model_dump()
    original_candidate = original.response_b.model_dump()
    swapped_candidate = swapped.response_a.model_dump()
    swapped_baseline = swapped.response_b.model_dump()
    return {
        "baseline": {
            name: round(
                (original_baseline[name] + swapped_baseline[name]) / 2,
                4,
            )
            for name in SCORE_FIELDS
        },
        "candidate": {
            name: round(
                (original_candidate[name] + swapped_candidate[name]) / 2,
                4,
            )
            for name in SCORE_FIELDS
        },
    }


def _category_counts(cases: Iterable[CalibrationCase]) -> dict[str, int]:
    counts = {item.value: 0 for item in CaseCategory}
    for case in cases:
        counts[case.category.value] += 1
    return counts


def _mean(values) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


_FIXED_TIME = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
