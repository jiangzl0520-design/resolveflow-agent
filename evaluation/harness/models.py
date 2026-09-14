from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CaseCategory(StrEnum):
    NORMAL = "normal"
    BOUNDARY = "boundary"
    FAILURE = "failure"
    ADVERSARIAL = "adversarial"


class GoldenCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    category: CaseCategory
    description: str = Field(min_length=5, max_length=300)
    input: dict[str, Any] = Field(min_length=1)
    expected: dict[str, Any] = Field(min_length=1)
    tags: tuple[str, ...] = ()


class GoldenDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(pattern=r"^1\.0$")
    dataset_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    dataset_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    task_type: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    description: str = Field(min_length=10, max_length=500)
    synthetic: bool
    cases: tuple[GoldenCase, ...] = Field(min_length=4)

    @model_validator(mode="after")
    def validate_case_collection(self) -> "GoldenDataset":
        identifiers = [case.case_id for case in self.cases]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Golden Dataset case_id values must be unique.")
        present = {case.category for case in self.cases}
        missing = set(CaseCategory) - present
        if missing:
            names = ", ".join(sorted(item.value for item in missing))
            raise ValueError(f"Golden Dataset is missing categories: {names}.")
        return self


class RunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    harness_version: str = "1.0.0"
    evaluator_name: str = "expected_field_match"
    evaluator_version: str = "1.0.0"
    baseline_subject: str = Field(min_length=1, max_length=100)
    candidate_subject: str = Field(min_length=1, max_length=100)
    seed: int = 20260805
    minimum_candidate_accuracy: float = Field(default=0.95, ge=0, le=1)
    maximum_regressions: int = Field(default=0, ge=0)


class SubjectCaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    actual: dict[str, Any] | None
    passed: bool
    error_code: str | None = None


class CaseComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    category: CaseCategory
    expected: dict[str, Any]
    baseline: SubjectCaseResult
    candidate: SubjectCaseResult


class CategoryScore(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    category: CaseCategory
    passed: int
    total: int
    accuracy: float


class SubjectSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: int
    total: int
    accuracy: float
    errors: int
    categories: tuple[CategoryScore, ...]


class EvalComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    accuracy_lift_points: float
    improvements: tuple[str, ...]
    regressions: tuple[str, ...]
    quality_gate_passed: bool


class EvalReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    report_schema_version: str = "1.0"
    run_id: str
    dataset_id: str
    dataset_version: str
    dataset_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    synthetic: bool
    task_type: str
    config_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    config: RunConfig
    baseline: SubjectSummary
    candidate: SubjectSummary
    comparison: EvalComparison
    cases: tuple[CaseComparison, ...]
