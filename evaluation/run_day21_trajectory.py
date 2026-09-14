from argparse import ArgumentParser
import json
from pathlib import Path
from typing import Sequence

from pydantic import ValidationError

from evaluation.day21_trajectory import (
    DIMENSIONS,
    FinalAnswerOnlyBaseline,
    ResolveFlowAgentSubject,
    TrajectoryRuleEvaluator,
)
from evaluation.harness import EvalHarness
from evaluation.harness.core import (
    DatasetValidationError,
    RunConfigurationError,
)
from evaluation.harness.models import RunConfig


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = (
    ROOT / "evaluation" / "datasets" / "day21_agent_trajectory_v1.json"
)
DEFAULT_CONFIG = (
    ROOT / "evaluation" / "configs" / "day21_trajectory_v1.json"
)
DEFAULT_OUTPUT = (
    ROOT / "evaluation" / "reports" / "day21_agent_trajectory_v1_report.json"
)


def run_evaluation(
    dataset_path: Path = DEFAULT_DATASET,
    *,
    config_path: Path = DEFAULT_CONFIG,
    output_path: Path | None = None,
    minimum_accuracy: float | None = None,
) -> dict:
    harness = EvalHarness()
    dataset = harness.load_dataset(dataset_path)
    config = harness.load_config(config_path)
    if minimum_accuracy is not None:
        config = RunConfig.model_validate(
            {
                **config.model_dump(mode="json"),
                "minimum_candidate_accuracy": minimum_accuracy,
            }
        )
    evaluator = TrajectoryRuleEvaluator()
    report = harness.run(
        dataset,
        config,
        baseline=FinalAnswerOnlyBaseline(),
        candidate=ResolveFlowAgentSubject(),
        evaluator=evaluator,
    )
    document = report.model_dump(mode="json")
    document["trajectory_summary"] = _trajectory_summary(
        dataset,
        report,
        evaluator,
    )
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(
                document,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return document


def _trajectory_summary(dataset, report, evaluator) -> dict:
    cases_by_id = {case.case_id: case for case in dataset.cases}
    result: dict = {}
    for subject_name in ("baseline", "candidate"):
        counts = {name: 0 for name in DIMENSIONS}
        lucky: list[str] = []
        attributed_domains: dict[str, int] = {}
        case_assessments = []
        for comparison in report.cases:
            subject_result = getattr(comparison, subject_name)
            actual = subject_result.actual or {}
            assessment = evaluator.assess(
                cases_by_id[comparison.case_id],
                actual,
            )
            for dimension, detail in assessment["dimensions"].items():
                counts[dimension] += int(detail["passed"])
            if (
                assessment["dimensions"]["task_outcome"]["passed"]
                and not assessment["overall_passed"]
            ):
                lucky.append(comparison.case_id)
            domain = str(actual.get("failure_domain", "missing"))
            attributed_domains[domain] = attributed_domains.get(domain, 0) + 1
            case_assessments.append(
                {
                    "case_id": comparison.case_id,
                    **assessment,
                }
            )
        total = len(report.cases)
        result[subject_name] = {
            "dimensions": {
                name: {
                    "passed": passed,
                    "total": total,
                    "accuracy": round(passed / total, 4),
                }
                for name, passed in counts.items()
            },
            "lucky_final_but_unsafe_cases": lucky,
            "attributed_domains": dict(sorted(attributed_domains.items())),
            "case_assessments": case_assessments,
        }
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--minimum-accuracy", type=float)
    args = parser.parse_args(argv)
    try:
        document = run_evaluation(
            args.dataset,
            config_path=args.config,
            output_path=args.output,
            minimum_accuracy=args.minimum_accuracy,
        )
    except DatasetValidationError as exc:
        print(json.dumps({"error_code": "dataset_invalid", "message": str(exc)}))
        return 2
    except (RunConfigurationError, ValidationError):
        print(json.dumps({"error_code": "run_config_invalid"}))
        return 2
    except OSError:
        print(json.dumps({"error_code": "report_persistence_failed"}))
        return 3
    print(json.dumps(document, ensure_ascii=False, indent=2))
    return 0 if document["comparison"]["quality_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
