from argparse import ArgumentParser
from datetime import UTC, datetime
import json
from uuid import UUID, uuid4

from app.core.config import Settings
from app.db.database import Database
from app.domain.auth import AuthenticatedActor, Role
from app.domain.grounded_answer import GroundedAnswerQuery
from app.domain.retrieval import RetrievalMode
from app.knowledge.constants import KNOWLEDGE_EMBEDDING_DIMENSIONS
from app.knowledge.embeddings import create_openai_embedding_runtime
from app.llm.runtime import create_openai_model_gateway_runtime
from app.repositories.sqlalchemy_knowledge_answer_repository import (
    SqlAlchemyKnowledgeAnswerRepository,
)
from app.repositories.sqlalchemy_knowledge_search_repository import (
    SqlAlchemyKnowledgeSearchRepository,
)
from app.services.grounded_answer_service import GroundedAnswerService
from app.services.knowledge_search_service import KnowledgeSearchService


class _KeywordOnlyProvider:
    model = "keyword-only"
    dimensions = KNOWLEDGE_EMBEDDING_DIMENSIONS

    def embed(self, texts):
        raise AssertionError("Keyword-only search must not request embeddings.")


def main() -> None:
    parser = ArgumentParser(
        description="Answer a policy question with verified citations."
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
    parser.add_argument("--question", required=True)
    parser.add_argument(
        "--mode",
        type=RetrievalMode,
        choices=list(RetrievalMode),
        default=RetrievalMode.HYBRID,
    )
    parser.add_argument("--candidate-k", type=int, default=10)
    parser.add_argument("--max-evidence", type=int, default=5)
    parser.add_argument("--as-of", type=datetime.fromisoformat)
    parser.add_argument("--request-id", default=None)
    parser.add_argument("--trace-id", default=None)
    arguments = parser.parse_args()

    settings = Settings.from_env()
    database = Database(settings.database_url)
    embedding_runtime = None
    model_runtime = None
    try:
        if arguments.mode is RetrievalMode.KEYWORD:
            embedding_provider = _KeywordOnlyProvider()
        else:
            embedding_runtime = create_openai_embedding_runtime(settings)
            embedding_provider = embedding_runtime.provider
        model_runtime = create_openai_model_gateway_runtime(
            settings,
            database.session_factory,
        )
        search_repository = SqlAlchemyKnowledgeSearchRepository(
            database.session_factory
        )
        result = GroundedAnswerService(
            KnowledgeSearchService(search_repository, embedding_provider),
            model_runtime.gateway,
            SqlAlchemyKnowledgeAnswerRepository(database.session_factory),
        ).answer(
            AuthenticatedActor(
                actor_id=arguments.actor_id,
                tenant_id=arguments.tenant_id,
                roles=frozenset(arguments.role),
            ),
            GroundedAnswerQuery(
                question=arguments.question,
                as_of=arguments.as_of or datetime.now(UTC),
                retrieval_mode=arguments.mode,
                candidate_k=arguments.candidate_k,
                max_evidence=arguments.max_evidence,
                request_id=arguments.request_id or f"cli-{uuid4()}",
                trace_id=arguments.trace_id or f"cli-{uuid4()}",
            ),
        )
        print(
            json.dumps(
                {
                    "run_id": str(result.run.id),
                    "retrieval_run_id": str(result.run.retrieval_run_id),
                    "status": result.status.value,
                    "answer": result.answer_text,
                    "citations": [
                        {
                            "citation_id": citation.citation_id,
                            "source_key": citation.source_key,
                            "version": citation.document_version,
                            "source": (
                                f"{citation.source_uri}:"
                                f"{citation.source_line_start}-"
                                f"{citation.source_line_end}"
                            ),
                            "exact_quote": citation.exact_quote,
                        }
                        for citation in result.citations
                    ],
                    "request_id": result.run.request_id,
                    "trace_id": result.run.trace_id,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        if model_runtime is not None:
            model_runtime.close()
        if embedding_runtime is not None:
            embedding_runtime.close()
        database.dispose()


if __name__ == "__main__":
    main()
