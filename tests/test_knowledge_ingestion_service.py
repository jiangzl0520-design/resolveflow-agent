from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.domain.knowledge import (
    IngestPolicyDocumentCommand,
    KnowledgeIngestionStatus,
)
from app.knowledge.errors import (
    KnowledgeIdempotencyConflictError,
    KnowledgeIngestionError,
    KnowledgeVersionConflictError,
)
from app.repositories.knowledge_repository import (
    InMemoryKnowledgeRepository,
)
from app.services.knowledge_ingestion_service import (
    KnowledgeIngestionService,
)

TENANT_ID = UUID("81000000-0000-0000-0000-000000000001")
OTHER_TENANT_ID = UUID("81000000-0000-0000-0000-000000000002")
NOW = datetime(2026, 8, 4, 9, 0, tzinfo=UTC)
SOURCE_V1 = """# 配送争议政策

## 签收争议

物流显示签收但客户否认收货时，必须查询签收证明。

## 退款要求

证据冲突时必须转人工审核，不能直接退款。
"""


def command(
    *,
    tenant_id: UUID = TENANT_ID,
    version: str = "2026.08",
    source_text: str = SOURCE_V1,
    idempotency_key: str = "knowledge-day12-key-001",
) -> IngestPolicyDocumentCommand:
    return IngestPolicyDocumentCommand(
        tenant_id=tenant_id,
        source_key="policy/cn/delivery-dispute",
        document_version=version,
        title="配送争议政策",
        source_uri=(
            f"repo://policies/delivery-dispute-{version}.md"
        ),
        source_text=source_text,
        effective_from=NOW,
        idempotency_key=idempotency_key,
        request_id=f"request-{idempotency_key}",
        trace_id=f"trace-{idempotency_key}",
    )


def service(repository: InMemoryKnowledgeRepository):
    return KnowledgeIngestionService(repository, clock=lambda: NOW)


def test_ingestion_persists_traceable_document_and_chunks() -> None:
    repository = InMemoryKnowledgeRepository()

    result = service(repository).ingest(command())

    assert result.run.status is KnowledgeIngestionStatus.COMPLETED
    assert result.document is not None
    assert result.document.raw_content == SOURCE_V1
    assert result.run.chunk_count == 2
    assert len(result.chunks) == 2
    for index, chunk in enumerate(result.chunks):
        assert chunk.tenant_id == TENANT_ID
        assert chunk.document_id == result.document.id
        assert chunk.chunk_index == index
        assert chunk.document_version == "2026.08"
        assert chunk.source_uri == result.document.source_uri
        assert chunk.effective_from == NOW
        assert chunk.source_line_start <= chunk.source_line_end
        assert chunk.section_path[0] == "配送争议政策"


def test_exact_idempotent_replay_creates_no_second_document() -> None:
    repository = InMemoryKnowledgeRepository()
    ingestion = service(repository)

    first = ingestion.ingest(command())
    replay = ingestion.ingest(command())

    assert replay.run.id == first.run.id
    assert replay.document is not None
    assert replay.document.id == first.document.id
    assert len(repository.runs) == 1
    assert len(repository.documents) == 1
    assert len(repository.chunks) == 2


def test_same_version_and_content_with_new_key_is_deduplicated() -> None:
    repository = InMemoryKnowledgeRepository()
    ingestion = service(repository)
    first = ingestion.ingest(command())

    duplicate = ingestion.ingest(
        command(idempotency_key="knowledge-day12-key-002")
    )

    assert duplicate.run.status is KnowledgeIngestionStatus.SKIPPED
    assert duplicate.deduplicated is True
    assert duplicate.document is not None
    assert duplicate.document.id == first.document.id
    assert len(repository.runs) == 2
    assert len(repository.documents) == 1


def test_line_endings_are_canonicalized_for_document_deduplication() -> None:
    repository = InMemoryKnowledgeRepository()
    ingestion = service(repository)
    first = ingestion.ingest(command())

    duplicate = ingestion.ingest(
        command(
            source_text=SOURCE_V1.replace("\n", "\r\n"),
            idempotency_key="knowledge-day12-key-003",
        )
    )

    assert duplicate.deduplicated is True
    assert duplicate.document is not None
    assert duplicate.document.id == first.document.id


def test_same_source_version_cannot_silently_change_content() -> None:
    repository = InMemoryKnowledgeRepository()
    ingestion = service(repository)
    ingestion.ingest(command())

    with pytest.raises(KnowledgeVersionConflictError):
        ingestion.ingest(
            command(
                source_text=SOURCE_V1 + "\n新增但未升级版本。",
                idempotency_key="knowledge-day12-key-004",
            )
        )

    failed = repository.get_run_by_idempotency_key(
        TENANT_ID,
        "knowledge-day12-key-004",
    )
    assert failed is not None
    assert failed.status is KnowledgeIngestionStatus.FAILED
    assert failed.error_code == "knowledge_version_content_conflict"
    assert len(repository.documents) == 1


def test_incremental_version_keeps_history_and_moves_current_pointer() -> None:
    repository = InMemoryKnowledgeRepository()
    ingestion = service(repository)
    first = ingestion.ingest(command())
    source_v2 = SOURCE_V1.replace("不能直接退款", "必须由主管批准")

    second = ingestion.ingest(
        command(
            version="2026.09",
            source_text=source_v2,
            idempotency_key="knowledge-day12-key-005",
        )
    )

    documents = sorted(
        repository.documents,
        key=lambda item: item.document_version,
    )
    assert len(documents) == 2
    assert documents[0].id == first.document.id
    assert documents[0].is_current is False
    assert documents[1].id == second.document.id
    assert documents[1].is_current is True
    assert {chunk.document_version for chunk in repository.chunks} == {
        "2026.08",
        "2026.09",
    }


def test_failed_publish_can_resume_with_same_run_and_no_duplicates() -> None:
    repository = InMemoryKnowledgeRepository()
    repository.fail_next_publish()
    ingestion = service(repository)

    with pytest.raises(KnowledgeIngestionError) as captured:
        ingestion.ingest(command())
    assert captured.value.code == "knowledge_storage_unavailable"
    failed = repository.runs[0]
    assert failed.status is KnowledgeIngestionStatus.FAILED
    assert failed.attempts == 1

    recovered = ingestion.ingest(command())

    assert recovered.run.id == failed.id
    assert recovered.run.attempts == 2
    assert recovered.run.status is KnowledgeIngestionStatus.COMPLETED
    assert len(repository.documents) == 1
    assert len(repository.chunks) == 2


def test_idempotency_key_cannot_be_reused_for_different_input() -> None:
    repository = InMemoryKnowledgeRepository()
    ingestion = service(repository)
    ingestion.ingest(command())

    with pytest.raises(KnowledgeIdempotencyConflictError):
        ingestion.ingest(
            command(source_text=SOURCE_V1 + "\n不同请求。")
        )

    assert len(repository.runs) == 1
    assert len(repository.documents) == 1


def test_source_version_and_idempotency_are_tenant_scoped() -> None:
    repository = InMemoryKnowledgeRepository()
    ingestion = service(repository)

    tenant_a = ingestion.ingest(command())
    tenant_b = ingestion.ingest(command(tenant_id=OTHER_TENANT_ID))

    assert tenant_a.document is not None
    assert tenant_b.document is not None
    assert tenant_a.document.id != tenant_b.document.id
    assert len(repository.documents) == 2

