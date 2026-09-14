from argparse import ArgumentParser
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import platform
from uuid import UUID

from app.domain.knowledge import IngestPolicyDocumentCommand
from app.knowledge.errors import (
    KnowledgeIngestionError,
    KnowledgeVersionConflictError,
)
from app.repositories.knowledge_repository import (
    InMemoryKnowledgeRepository,
)
from app.services.knowledge_ingestion_service import (
    KnowledgeIngestionService,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = (
    ROOT
    / "evaluation"
    / "datasets"
    / "day12_knowledge_ingestion_v1.json"
)
TENANT_ID = UUID("e2000000-0000-0000-0000-000000000001")
EFFECTIVE_FROM = datetime(2026, 8, 1, tzinfo=UTC)


def _source(case_id: str, *, changed: bool = False) -> str:
    final_rule = (
        "证据冲突时必须升级人工并冻结自动退款。"
        if changed
        else "证据冲突时必须升级人工审核。"
    )
    return (
        f"# 配送争议政策 {case_id}\n\n"
        "## 签收证明\n\n"
        "显示签收但客户否认收货时，必须查询签收证明。\n\n"
        "## 退款限制\n\n"
        f"{final_rule}"
    )


def _command(
    case_id: str,
    *,
    version: str = "1.0.0",
    idempotency_suffix: str = "initial",
    changed: bool = False,
) -> IngestPolicyDocumentCommand:
    return IngestPolicyDocumentCommand(
        tenant_id=TENANT_ID,
        source_key=f"policy/evaluation/{case_id}",
        document_version=version,
        title=f"配送争议政策 {case_id}",
        source_uri=f"repo://policies/{case_id}.md",
        source_text=_source(case_id, changed=changed),
        effective_from=EFFECTIVE_FROM,
        effective_to=EFFECTIVE_FROM + timedelta(days=365),
        idempotency_key=f"day12-{case_id}-{idempotency_suffix}",
        request_id=f"request-{case_id}-{idempotency_suffix}",
        trace_id=f"trace-{case_id}-{idempotency_suffix}",
    )


def _traceability_cases(count: int) -> dict[str, int]:
    repository = InMemoryKnowledgeRepository()
    service = KnowledgeIngestionService(repository)
    complete_chunks = 0
    total_chunks = 0
    for index in range(count):
        result = service.ingest(_command(f"lineage-{index:03d}"))
        assert result.document is not None
        for chunk in result.chunks:
            total_chunks += 1
            parent = repository.get_document(
                chunk.tenant_id,
                chunk.document_id,
            )
            complete_chunks += int(
                parent is not None
                and parent.source_key.startswith("policy/evaluation/")
                and chunk.source_uri == parent.source_uri
                and chunk.document_version == parent.document_version
                and chunk.tenant_id == parent.tenant_id
                and chunk.effective_from == parent.effective_from
                and chunk.effective_to == parent.effective_to
                and chunk.source_line_start <= chunk.source_line_end
                and bool(chunk.section_path)
            )
    return {
        "documents": len(repository.documents),
        "total_chunks": total_chunks,
        "complete_chunks": complete_chunks,
    }


def _duplicate_cases(count: int) -> dict[str, int]:
    repository = InMemoryKnowledgeRepository()
    service = KnowledgeIngestionService(repository)
    blocked = 0
    for index in range(count):
        case_id = f"duplicate-{index:03d}"
        first = service.ingest(_command(case_id))
        duplicate = service.ingest(
            _command(case_id, idempotency_suffix="redelivery")
        )
        blocked += int(
            duplicate.deduplicated
            and duplicate.document is not None
            and first.document is not None
            and duplicate.document.id == first.document.id
        )
    return {
        "cases": count,
        "duplicates_blocked": blocked,
        "documents_created": len(repository.documents),
    }


def _version_drift_cases(count: int) -> dict[str, int]:
    repository = InMemoryKnowledgeRepository()
    service = KnowledgeIngestionService(repository)
    blocked = 0
    for index in range(count):
        case_id = f"drift-{index:03d}"
        service.ingest(_command(case_id))
        try:
            service.ingest(
                _command(
                    case_id,
                    idempotency_suffix="changed",
                    changed=True,
                )
            )
        except KnowledgeVersionConflictError:
            blocked += 1
    return {
        "cases": count,
        "drifts_blocked": blocked,
        "documents_created": len(repository.documents),
    }


def _incremental_cases(count: int) -> dict[str, int]:
    successful = 0
    retained_old_versions = 0
    for index in range(count):
        repository = InMemoryKnowledgeRepository()
        service = KnowledgeIngestionService(repository)
        case_id = f"incremental-{index:03d}"
        first = service.ingest(_command(case_id))
        second = service.ingest(
            _command(
                case_id,
                version="2.0.0",
                idempotency_suffix="v2",
                changed=True,
            )
        )
        old = repository.find_document_by_source_version(
            TENANT_ID,
            f"policy/evaluation/{case_id}",
            "1.0.0",
        )
        current = repository.find_document_by_source_version(
            TENANT_ID,
            f"policy/evaluation/{case_id}",
            "2.0.0",
        )
        successful += int(
            first.document is not None
            and second.document is not None
            and old is not None
            and current is not None
            and not old.is_current
            and current.is_current
        )
        retained_old_versions += int(
            old is not None
            and bool(repository.list_chunks(TENANT_ID, old.id))
        )
    return {
        "cases": count,
        "current_pointer_updates": successful,
        "old_versions_retained": retained_old_versions,
    }


def _rerun_cases(count: int) -> dict[str, int]:
    recovered = 0
    without_partial_documents = 0
    for index in range(count):
        repository = InMemoryKnowledgeRepository()
        service = KnowledgeIngestionService(repository)
        command = _command(f"rerun-{index:03d}")
        repository.fail_next_publish()
        try:
            service.ingest(command)
        except KnowledgeIngestionError:
            without_partial_documents += int(not repository.documents)
        result = service.ingest(command)
        recovered += int(
            result.run.attempts == 2
            and len(repository.documents) == 1
            and result.document is not None
        )
    return {
        "cases": count,
        "recovered_on_same_run": recovered,
        "failures_without_partial_documents": without_partial_documents,
    }


def run(dataset_path: Path) -> dict[str, object]:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    counts = dataset["scenario_counts"]
    traceability = _traceability_cases(counts["traceable_ingestion"])
    duplicate = _duplicate_cases(counts["duplicate_ingestion"])
    drift = _version_drift_cases(counts["same_version_drift"])
    incremental = _incremental_cases(counts["incremental_update"])
    rerun = _rerun_cases(counts["failed_publish_rerun"])
    case_count = sum(counts.values())
    return {
        "report_id": "day12-knowledge-ingestion-v1",
        "dataset": {**dataset, "case_count": case_count},
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "repository": "deterministic in-memory evaluation adapter",
            "paid_api_calls": 0,
        },
        "traceability": {
            **traceability,
            "complete_percent": round(
                traceability["complete_chunks"]
                / traceability["total_chunks"]
                * 100,
                2,
            ),
        },
        "deduplication": {
            **duplicate,
            "blocked_percent": round(
                duplicate["duplicates_blocked"]
                / duplicate["cases"]
                * 100,
                2,
            ),
        },
        "same_version_drift": {
            **drift,
            "blocked_percent": round(
                drift["drifts_blocked"] / drift["cases"] * 100,
                2,
            ),
        },
        "incremental_update": incremental,
        "failed_publish_rerun": rerun,
        "measured_value": {
            "traceable_chunk_percent": round(
                traceability["complete_chunks"]
                / traceability["total_chunks"]
                * 100,
                2,
            ),
            "duplicate_documents_avoided": duplicate[
                "duplicates_blocked"
            ],
            "silent_same_version_overwrites_prevented": drift[
                "drifts_blocked"
            ],
            "failed_runs_recovered_without_partial_documents": min(
                rerun["recovered_on_same_run"],
                rerun["failures_without_partial_documents"],
            ),
        },
        "interpretation_limits": [
            "All evaluation documents and failures are synthetic and deterministic.",
            "The report measures ingestion integrity, not retrieval relevance or answer quality.",
            "Real PostgreSQL behavior is covered separately by the postgres integration suite.",
            "No production traffic, business revenue, or model-quality improvement is claimed.",
        ],
    }


def main() -> None:
    parser = ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    report = run(arguments.dataset)
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)


if __name__ == "__main__":
    main()
