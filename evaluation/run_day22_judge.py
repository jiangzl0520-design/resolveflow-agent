from argparse import ArgumentParser
import json
from pathlib import Path
from typing import Sequence

from evaluation.day22_calibration import (
    CalibrationLoadError,
    load_calibration_config,
    load_calibration_dataset,
    load_judge_rubric,
    run_calibration,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = (
    ROOT / "evaluation" / "datasets" / "day22_judge_calibration_v1.json"
)
DEFAULT_CONFIG = (
    ROOT / "evaluation" / "configs" / "day22_judge_calibration_v1.json"
)
DEFAULT_RUBRIC = (
    ROOT / "evaluation" / "rubrics" / "day22_explanation_quality_v1.json"
)
DEFAULT_OUTPUT = (
    ROOT / "evaluation" / "reports" / "day22_judge_calibration_v1_report.json"
)


def run_evaluation(
    dataset_path: Path = DEFAULT_DATASET,
    *,
    config_path: Path = DEFAULT_CONFIG,
    rubric_path: Path = DEFAULT_RUBRIC,
    output_path: Path | None = None,
) -> dict:
    report = run_calibration(
        load_calibration_dataset(dataset_path),
        load_calibration_config(config_path),
        load_judge_rubric(rubric_path),
    )
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(
                report,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--rubric", type=Path, default=DEFAULT_RUBRIC)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        report = run_evaluation(
            args.dataset,
            config_path=args.config,
            rubric_path=args.rubric,
            output_path=args.output,
        )
    except CalibrationLoadError as exc:
        print(
            json.dumps(
                {"error_code": "calibration_artifact_invalid", "message": str(exc)}
            )
        )
        return 2
    except OSError:
        print(json.dumps({"error_code": "report_persistence_failed"}))
        return 3
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["combined_quality_gate"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
