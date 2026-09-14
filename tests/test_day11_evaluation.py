from pathlib import Path

from evaluation.run_day11_mcp_interoperability import run


def test_day11_mcp_report_has_reproducible_value_metrics() -> None:
    root = Path(__file__).resolve().parents[1]
    report = run(
        root
        / "evaluation"
        / "datasets"
        / "day11_mcp_interoperability_v1.json"
    )

    assert report["dataset"]["case_count"] == 60
    assert report["interoperability"]["local_remote_parity_matches"] == 30
    assert report["interoperability"]["parity_percent"] == 100.0
    assert (
        report["authorization_and_tenant_isolation"][
            "unauthorized_successes"
        ]
        == 0
    )
    assert report["contract_drift"]["detected_before_agent_exposure"] == 10
    assert report["dependency_outage"]["remote_calls_during_outage"] == 3
    assert report["dependency_outage"]["downstream_calls_avoided"] == 7
    assert report["dependency_outage"]["recovery_probe_succeeded"] is True

