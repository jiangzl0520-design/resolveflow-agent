from argparse import ArgumentParser
from datetime import datetime
import json
from pathlib import Path
from uuid import UUID

from app.core.config import Settings
from app.db.database import Database
from app.domain.knowledge import IngestPolicyDocumentCommand
from app.repositories.sqlalchemy_knowledge_repository import (
    SqlAlchemyKnowledgeRepository,
)
from app.services.knowledge_ingestion_service import (
    KnowledgeIngestionService,
)


def main() -> None:
    parser = ArgumentParser(
        description="Ingest one tenant-scoped Markdown policy document."
    )
    parser.add_argument("--tenant-id", type=UUID, required=True)
    parser.add_argument("--source-key", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--source-uri", required=True)
    parser.add_argument("--effective-from", type=datetime.fromisoformat, required=True)
    parser.add_argument("--effective-to", type=datetime.fromisoformat)
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--trace-id", required=True)
    parser.add_argument("--file", type=Path, required=True)
    arguments = parser.parse_args()

    source_text = arguments.file.read_text(encoding="utf-8")
    settings = Settings.from_env()
    database = Database(settings.database_url)
    try:
        result = KnowledgeIngestionService(
            SqlAlchemyKnowledgeRepository(database.session_factory)
        ).ingest(
            IngestPolicyDocumentCommand(
                tenant_id=arguments.tenant_id,
                source_key=arguments.source_key,
                document_version=arguments.version,
                title=arguments.title,
                source_uri=arguments.source_uri,
                source_text=source_text,
                effective_from=arguments.effective_from,
                effective_to=arguments.effective_to,
                idempotency_key=arguments.idempotency_key,
                request_id=arguments.request_id,
                trace_id=arguments.trace_id,
            )
        )
        print(
            json.dumps(
                {
                    "run_id": str(result.run.id),
                    "status": result.run.status.value,
                    "document_id": (
                        str(result.document.id)
                        if result.document is not None
                        else None
                    ),
                    "content_hash": result.run.content_hash,
                    "chunk_count": result.run.chunk_count,
                    "deduplicated": result.deduplicated,
                    "request_id": result.run.request_id,
                    "trace_id": result.run.trace_id,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        database.dispose()


if __name__ == "__main__":
    main()

