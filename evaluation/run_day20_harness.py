from argparse import ArgumentParser
import json
from pathlib import Path
from typing import Sequence

from pydantic import ValidationError

from evaluation.day20_subjects import KeywordBaseline, TrustedFactCandidate
from evaluation.harness import EvalHarness, exit_code_for
from evaluation.harness.core import (
    DatasetValidationError,
    RunConfigurationError,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "evaluation" / "datasets" / "day20_golden_v1.json"
DEFAULT_CONFIG = ROOT / "evaluation" / "configs" / "day20_offline_v1.json"
DEFAULT_OUTPUT = ROOT / "evaluation" / "reports" / "day20_golden_v1_report.json"


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
        from evaluation.harness.models import RunConfig

        config = RunConfig.model_validate(
            {
                **config.model_dump(mode="json"),
                "minimum_candidate_accuracy": minimum_accuracy,
            }
        )
    report = harness.run(
        dataset,
        config,
        baseline=KeywordBaseline(),
        candidate=TrustedFactCandidate(),
    )
    if output_path is not None:
        harness.write_report(report, output_path)
    return report.model_dump(mode="json")


def main(argv: Sequence[str] | None = None) -> int:
    parser = ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--minimum-accuracy", type=float)
    args = parser.parse_args(argv)
    try:
        report = run_evaluation(
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
    print(json.dumps(report, ensure_ascii=False, indent=2))
    from evaluation.harness.models import EvalReport

    return exit_code_for(EvalReport.model_validate(report))


if __name__ == "__main__":
    raise SystemExit(main())
