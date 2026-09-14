from __future__ import annotations

import json
from pathlib import Path

from app.observability.metrics import (
    ResolveFlowMetrics,
    diagnose_agent_success_drop,
)


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evaluation" / "datasets" / "day19_incident_diagnosis_v1.json"
REPORT = ROOT / "evaluation" / "reports" / "day19_incident_diagnosis_v1_report.json"


def _window(specification: dict, *, preserve_domains: bool) -> ResolveFlowMetrics:
    metrics = ResolveFlowMetrics()
    for _ in range(specification["completed"]):
        metrics.record_agent_run(
            workflow="agent_runner", status="completed",
            termination_reason="completed", failure_domain="none",
            duration=0.4, steps=3, input_tokens=100, output_tokens=20,
        )
    for domain, count in specification["failures"].items():
        for _ in range(count):
            metrics.record_agent_run(
                workflow="agent_runner", status="failed",
                termination_reason="tool_runtime_error",
                failure_domain=domain if preserve_domains else "unknown",
                duration=0.8, steps=4, input_tokens=140, output_tokens=25,
            )
    return metrics


def run_evaluation(*, write_report: bool = True) -> dict:
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    baseline_window = _window(dataset["baseline_window"], preserve_domains=True)
    aggregate_only = _window(dataset["incident_window"], preserve_domains=False)
    attributed = _window(dataset["incident_window"], preserve_domains=True)
    baseline_diagnosis = diagnose_agent_success_drop(baseline_window, aggregate_only)
    final_diagnosis = diagnose_agent_success_drop(baseline_window, attributed)
    expected = dataset["expected"]
    report = {
        "dataset_id": dataset["dataset_id"],
        "data_kind": dataset["data_kind"],
        "sample_size": 200,
        "baseline": {
            "method": "success_rate_without_failure_domain",
            "detects_drop": baseline_diagnosis.success_rate_change < 0,
            "top_failure_domain": baseline_diagnosis.top_failure_domain,
            "correct_cause": baseline_diagnosis.top_failure_domain == expected["top_failure_domain"],
        },
        "final": {
            "method": "low_cardinality_failure_domain_metrics",
            "baseline_success_rate": final_diagnosis.baseline_success_rate,
            "current_success_rate": final_diagnosis.current_success_rate,
            "success_rate_change_percentage_points": round(final_diagnosis.success_rate_change * 100, 2),
            "top_failure_domain": final_diagnosis.top_failure_domain,
            "top_failure_count": final_diagnosis.top_failure_count,
            "attributed_failure_share": round(final_diagnosis.attributed_failure_share, 4),
            "correct_cause": final_diagnosis.top_failure_domain == expected["top_failure_domain"],
        },
        "value_statement": (
            "The aggregate baseline detects a 25-point regression but cannot explain it; "
            "the final metric contract attributes 20 of 30 failures (66.67%) to tools."
        ),
        "production_claim": False,
    }
    if write_report:
        REPORT.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return report


if __name__ == "__main__":
    print(json.dumps(run_evaluation(), ensure_ascii=False, indent=2))
