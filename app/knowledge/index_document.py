from argparse import ArgumentParser
import json
from uuid import UUID

from app.core.config import Settings
from app.db.database import Database
from app.knowledge.embeddings import create_openai_embedding_runtime
from app.repositories.sqlalchemy_knowledge_repository import (
    SqlAlchemyKnowledgeRepository,
)
from app.repositories.sqlalchemy_knowledge_search_repository import (
    SqlAlchemyKnowledgeSearchRepository,
)
from app.services.knowledge_indexing_service import KnowledgeIndexingService


def main() -> None:
    parser = ArgumentParser(
        description="Create embeddings for one already-ingested policy document."
    )
    parser.add_argument("--tenant-id", type=UUID, required=True)
    parser.add_argument("--document-id", type=UUID, required=True)
    arguments = parser.parse_args()

    settings = Settings.from_env()
    database = Database(settings.database_url)
    runtime = create_openai_embedding_runtime(settings)
    try:
        result = KnowledgeIndexingService(
            SqlAlchemyKnowledgeRepository(database.session_factory),
            SqlAlchemyKnowledgeSearchRepository(database.session_factory),
            runtime.provider,
        ).index_document(arguments.tenant_id, arguments.document_id)
        print(
            json.dumps(
                {
                    "tenant_id": str(result.tenant_id),
                    "document_id": str(result.document_id),
                    "embedding_model": result.embedding_model,
                    "indexed_chunk_count": result.indexed_chunk_count,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        runtime.close()
        database.dispose()


if __name__ == "__main__":
    main()
