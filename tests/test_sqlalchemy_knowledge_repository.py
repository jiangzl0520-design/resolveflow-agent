from datetime import UTC, datetime
from uuid import UUID

from app.db.database import Database
from app.domain.knowledge import IngestPolicyDocumentCommand
from app.repositories.sqlalchemy_knowledge_repository import (
    SqlAlchemyKnowledgeRepository,
)
from app.services.knowledge_ingestion_service import (
    KnowledgeIngestionService,
)
from tests.integration.database_helpers import migrated_sqlite_url

TENANT_ID = UUID("82000000-0000-0000-0000-000000000001")
OTHER_TENANT_ID = UUID("82000000-0000-0000-0000-000000000002")
NOW = datetime(2026, 8, 4, 10, 0, tzinfo=UTC)


def test_sqlalchemy_round_trip_preserves_chunk_lineage(tmp_path) -> None:
    database = Database(migrated_sqlite_url(tmp_path))
    repository = SqlAlchemyKnowledgeRepository(database.session_factory)
    command = IngestPolicyDocumentCommand(
        tenant_id=TENANT_ID,
        source_key="policy/cn/refund",
        document_version="1.0.0",
        title="退款政策",
        source_uri="repo://policies/refund-v1.md",
        source_text=(
            "# 退款政策\n\n## 审批\n\n"
            "所有退款必须经过资格检查和人工审批。"
        ),
        effective_from=NOW,
        idempotency_key="sqlite-knowledge-day12-001",
        request_id="request-sqlite-knowledge",
        trace_id="trace-sqlite-knowledge",
    )
    result = KnowledgeIngestionService(
        repository,
        clock=lambda: NOW,
    ).ingest(command)
    assert result.document is not None

    reloaded_repository = SqlAlchemyKnowledgeRepository(
        database.session_factory
    )
    document = reloaded_repository.get_document(
        TENANT_ID,
        result.document.id,
    )
    chunks = reloaded_repository.list_chunks(
        TENANT_ID,
        result.document.id,
    )

    assert document == result.document
    assert chunks == list(result.chunks)
    assert chunks[0].section_path == ("退款政策", "审批")
    assert chunks[0].source_line_start == 3
    assert chunks[0].document_version == "1.0.0"
    assert reloaded_repository.get_document(
        OTHER_TENANT_ID,
        result.document.id,
    ) is None
    assert reloaded_repository.list_chunks(
        OTHER_TENANT_ID,
        result.document.id,
    ) == []
    database.dispose()

