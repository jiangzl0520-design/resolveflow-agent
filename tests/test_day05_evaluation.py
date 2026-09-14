from pathlib import Path

from evaluation.run_day05_reliability import run

ROOT = Path(__file__).resolve().parents[1]


def test_day05_reliability_report_is_reproducible() -> None:
    report = run(
        ROOT
        / "evaluation"
        / "datasets"
        / "day05_duplicate_delivery_v1.json"
    )
    baseline = report["baseline_without_idempotent_claim"]
    durable = report["resolveflow_durable_job_engine"]

    assert baseline["deliveries"] == durable["deliveries"] == 75
    assert baseline["duplicate_side_effects"] == 50
    assert durable["successful_jobs"] == 25
    assert durable["side_effects"] == 25
    assert durable["duplicate_side_effects"] == 0
    assert durable["jobs_with_exactly_one_side_effect"] == 25
    assert durable["expired_lease_jobs_recovered"] == 5
