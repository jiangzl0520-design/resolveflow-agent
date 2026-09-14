from pathlib import Path

from evaluation.run_day12_knowledge_ingestion import run


def test_day12_report_has_reproducible_integrity_metrics() -> None:
    root = Path(__file__).resolve().parents[1]
    report = run(
        root
        / "evaluation"
        / "datasets"
        / "day12_knowledge_ingestion_v1.json"
    )

    assert report["dataset"]["case_count"] == 60
    assert report["traceability"]["complete_percent"] == 100.0
    assert report["deduplication"]["duplicates_blocked"] == 15
    assert report["deduplication"]["documents_created"] == 15
    assert report["same_version_drift"]["drifts_blocked"] == 10
    assert report["same_version_drift"]["documents_created"] == 10
    assert report["incremental_update"]["current_pointer_updates"] == 10
    assert report["incremental_update"]["old_versions_retained"] == 10
    assert report["failed_publish_rerun"]["recovered_on_same_run"] == 5
    assert (
        report["failed_publish_rerun"][
            "failures_without_partial_documents"
        ]
        == 5
    )
