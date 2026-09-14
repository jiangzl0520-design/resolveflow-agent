from argparse import ArgumentParser
from datetime import UTC, datetime
import json
from uuid import UUID, uuid4

from app.core.config import Settings
from app.db.database import Database
from app.domain.auth import AuthenticatedActor, Role
from app.domain.retrieval import KnowledgeSearchQuery, RetrievalMode
from app.knowledge.constants import KNOWLEDGE_EMBEDDING_DIMENSIONS
from app.knowledge.embeddings import create_openai_embedding_runtime
from app.repositories.sqlalchemy_knowledge_search_repository import (
    SqlAlchemyKnowledgeSearchRepository,
)
from app.services.knowledge_search_service import KnowledgeSearchService


class _KeywordOnlyProvider:
    model = "keyword-only"
    dimensions = KNOWLEDGE_EMBEDDING_DIMENSIONS

    def embed(self, texts):
        raise AssertionError("Keyword-only search must not request embeddings.")


def main() -> None:
    parser = ArgumentParser(
        description="Search tenant-scoped policy knowledge."
    )
    parser.add_argument("--tenant-id", type=UUID, required=True)
    parser.add_argument("--actor-id", required=True)
    parser.add_argument(
        "--role",
        action="append",
        type=Role,
        required=True,
        choices=list(Role),
    )
    parser.add_argument("--query", required=True)
    parser.add_argument(
        "--mode",
        type=RetrievalMode,
        choices=list(RetrievalMode),
        default=RetrievalMode.HYBRID,
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--as-of", type=datetime.fromisoformat)
    parser.add_argument("--request-id", default=None)
    parser.add_argument("--trace-id", default=None)
    arguments = parser.parse_args()

    settings = Settings.from_env()
    database = Database(settings.database_url)
    runtime = None
    try:
        if arguments.mode is RetrievalMode.KEYWORD:
            provider = _KeywordOnlyProvider()
        else:
            runtime = create_openai_embedding_runtime(settings)
            provider = runtime.provider
        result = KnowledgeSearchService(
            SqlAlchemyKnowledgeSearchRepository(database.session_factory),
            provider,
        ).search(
            AuthenticatedActor(
                actor_id=arguments.actor_id,
                tenant_id=arguments.tenant_id,
                roles=frozenset(arguments.role),
            ),
            KnowledgeSearchQuery(
                text=arguments.query,
                as_of=arguments.as_of or datetime.now(UTC),
                top_k=arguments.top_k,
                mode=arguments.mode,
                request_id=arguments.request_id or f"cli-{uuid4()}",
                trace_id=arguments.trace_id or f"cli-{uuid4()}",
            ),
        )
        print(
            json.dumps(
                {
                    "run_id": str(result.run.id),
                    "mode": result.run.search_mode.value,
                    "degraded_reason": result.run.degraded_reason,
                    "rewritten_terms": result.rewritten_query.keyword_terms,
                    "hits": [
                        {
                            "rank": rank,
                            "source_key": hit.source_key,
                            "title": hit.title,
                            "citation": (
                                f"{hit.source_uri}:"
                                f"{hit.source_line_start}-"
                                f"{hit.source_line_end}"
                            ),
                            "semantic_rank": hit.semantic_rank,
                            "keyword_rank": hit.keyword_rank,
                            "fused_score": hit.fused_score,
                        }
                        for rank, hit in enumerate(result.hits, start=1)
                    ],
                    "request_id": result.run.request_id,
                    "trace_id": result.run.trace_id,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        if runtime is not None:
            runtime.close()
        database.dispose()


if __name__ == "__main__":
    main()
