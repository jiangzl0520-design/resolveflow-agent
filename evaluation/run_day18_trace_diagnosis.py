from argparse import ArgumentParser
import json
from pathlib import Path
from statistics import mean
from typing import Any

from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from app.core.config import AppEnvironment, Settings
from app.observability.diagnostics import TraceFailureAnalyzer
from app.observability.tracing import (
    FailureDomain,
    configure_global_tracing,
    mark_span_error,
    operation_span,
    set_safe_attributes,
)

ROOT = Path(__file__).resolve().parent
DEFAULT_DATASET = ROOT / "datasets" / "day18_trace_diagnosis_v1.json"
DEFAULT_OUTPUT = ROOT / "reports" / "day18_trace_diagnosis_v1_report.json"
_EXPORTER: InMemorySpanExporter | None = None


def _exporter() -> InMemorySpanExporter:
    global _EXPORTER
    if _EXPORTER is None:
        _EXPORTER = InMemorySpanExporter()
        configure_global_tracing(
            Settings(
                database_url="sqlite+pysqlite:///:memory:",
                environment=AppEnvironment.TEST,
                jwt_secret=(
                    "day18-evaluation-jwt-secret-at-least-32-characters"
                ),
            ),
            exporter=_EXPORTER,
        )
    return _EXPORTER


def load_cases(path: Path = DEFAULT_DATASET) -> tuple[dict[str, Any], ...]:
    document = json.loads(path.read_text(encoding="utf-8"))
    count = int(document["cases_per_template"])
    return tuple(
        {
            "case_id": f"{template['id_prefix']}-{index:02d}",
            "expected_domain": template["expected_domain"],
            "error_code": template["error_code"],
            "payload": template["payload"],
        }
        for template in document["templates"]
        for index in range(1, count + 1)
    )


def run_evaluation(path: Path = DEFAULT_DATASET) -> dict[str, Any]:
    cases = load_cases(path)
    exporter = _exporter()
    analyzer = TraceFailureAnalyzer()
    case_results: list[dict[str, Any]] = []
    for case in cases:
        exporter.clear()
        expected = case["expected_domain"]
        with operation_span(
            "run after-sales agent",
            attributes={"resolveflow.component": "agent"},
        ) as root:
            if expected is not None:
                with operation_span(
                    f"execute {expected} component",
                    attributes={
                        "resolveflow.component": expected,
                        "resolveflow.test.payload": case["payload"],
                    },
                ) as component:
                    mark_span_error(
                        component,
                        FailureDomain(expected),
                        case["error_code"],
                    )
                mark_span_error(
                    root,
                    FailureDomain.AGENT,
                    "agent_run_failed",
                )
            else:
                set_safe_attributes(
                    root,
                    {"resolveflow.test.payload": case["payload"]},
                )
        spans = list(exporter.get_finished_spans())
        diagnosis = analyzer.diagnose(spans)
        final_domain = diagnosis.domain.value if diagnosis else None
        final_code = diagnosis.error_code if diagnosis else None
        baseline_domain = None if expected is None else "unknown"
        baseline_correct = baseline_domain == expected
        final_correct = (
            final_domain == expected
            and (
                expected is None
                or final_code == case["error_code"]
            )
        )
        final_attributes = repr(
            [dict(span.attributes or {}) for span in spans]
        )
        baseline_exposed = _contains_secret(case["payload"])
        final_exposed = _contains_secret(final_attributes)
        case_results.append(
            {
                "case_id": case["case_id"],
                "expected_domain": expected,
                "expected_error_code": case["error_code"],
                "baseline_domain": baseline_domain,
                "baseline_correct": baseline_correct,
                "baseline_sensitive_exposure": baseline_exposed,
                "final_domain": final_domain,
                "final_error_code": final_code,
                "final_span_name": diagnosis.span_name if diagnosis else None,
                "final_depth": diagnosis.depth if diagnosis else None,
                "final_correct": final_correct,
                "final_sensitive_exposure": final_exposed,
                "span_count": len(spans),
            }
        )

    total = len(case_results)
    failure_count = sum(item["expected_domain"] is not None for item in case_results)
    baseline_correct_count = sum(item["baseline_correct"] for item in case_results)
    final_correct_count = sum(item["final_correct"] for item in case_results)
    baseline_accuracy = _percent(baseline_correct_count, total)
    final_accuracy = _percent(final_correct_count, total)
    document = json.loads(path.read_text(encoding="utf-8"))
    return {
        "dataset_version": document["dataset_version"],
        "synthetic": True,
        "execution": {
            "paid_model_calls": 0,
            "external_trace_backend_required": False,
            "exporter": "OpenTelemetry InMemorySpanExporter",
            "diagnosis": "deterministic deepest-classified-span",
        },
        "sample": {
            "total_cases": total,
            "failure_cases": failure_count,
            "benign_cases": total - failure_count,
            "failure_domains": sorted(
                {
                    item["expected_domain"]
                    for item in case_results
                    if item["expected_domain"] is not None
                }
            ),
        },
        "correlation_id_only_baseline": {
            "correct_cases": baseline_correct_count,
            "root_cause_accuracy_percent": baseline_accuracy,
            "sensitive_exposures": sum(
                item["baseline_sensitive_exposure"]
                for item in case_results
            ),
        },
        "opentelemetry_component_trace": {
            "correct_cases": final_correct_count,
            "root_cause_accuracy_percent": final_accuracy,
            "unclassified_failures": sum(
                item["expected_domain"] is not None
                and item["final_domain"] is None
                for item in case_results
            ),
            "sensitive_exposures": sum(
                item["final_sensitive_exposure"]
                for item in case_results
            ),
            "false_failure_diagnoses": sum(
                item["expected_domain"] is None
                and item["final_domain"] is not None
                for item in case_results
            ),
            "mean_spans_per_case": round(
                mean(item["span_count"] for item in case_results),
                2,
            ),
        },
        "measured_value": {
            "root_cause_accuracy_lift_points": round(
                final_accuracy - baseline_accuracy,
                2,
            ),
            "additional_failures_localized": (
                final_correct_count - baseline_correct_count
            ),
            "sensitive_exposures_prevented": sum(
                item["baseline_sensitive_exposure"]
                and not item["final_sensitive_exposure"]
                for item in case_results
            ),
        },
        "limitations": [
            "The dataset is synthetic and deterministic, not production traffic.",
            "The baseline represents correlation IDs plus a generic terminal error, not a commercial APM product.",
            "Latency and exporter network overhead are intentionally deferred to the performance phase.",
        ],
        "cases": case_results,
    }


def _contains_secret(value: str) -> bool:
    return "customer@example.com" in value or "TopSecret18" in value


def _percent(numerator: int, denominator: int) -> float:
    return round(numerator / denominator * 100, 2) if denominator else 0.0


def main() -> None:
    parser = ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = run_evaluation(args.dataset)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
