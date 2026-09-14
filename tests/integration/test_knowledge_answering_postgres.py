from datetime import UTC, datetime, timedelta
from os import environ
from urllib.parse import urlparse
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.db.database import Database
from app.db.models import (
    KnowledgeAnswerCitationRecord,
    KnowledgeAnswerRunRecord,
    KnowledgeRetrievalRunRecord,
    ModelCallRecordModel,
)
from app.domain.auth import AuthenticatedActor, Role
from app.domain.grounded_answer import GroundedAnswerQuery, KnowledgeAnswerStatus
from app.domain.knowledge import IngestPolicyDocumentCommand
from app.domain.retrieval import RetrievalMode
from app.knowledge.constants import KNOWLEDGE_EMBEDDING_DIMENSIONS
from app.llm.contracts import RawProviderResponse
from app.llm.fake_provider import FakeLLMProvider
from app.llm.gateway import ModelGateway
from app.repositories.sqlalchemy_knowledge_answer_repository import (
    SqlAlchemyKnowledgeAnswerRepository,
)
from app.repositories.sqlalchemy_knowledge_repository import (
    SqlAlchemyKnowledgeRepository,
)
from app.repositories.sqlalchemy_knowledge_search_repository import (
    SqlAlchemyKnowledgeSearchRepository,
)
from app.repositories.sqlalchemy_model_call_repository import (
    SqlAlchemyModelCallRepository,
)
from app.services.grounded_answer_service import GroundedAnswerService
from app.services.knowledge_indexing_service import KnowledgeIndexingService
from app.services.knowledge_ingestion_service import KnowledgeIngestionService
from app.services.knowledge_search_service import KnowledgeSearchService
from tests.integration.database_helpers import upgrade_database

TEST_DATABASE_URL = environ.get("TEST_DATABASE_URL")
TENANT_ID = UUID("84000000-0000-0000-0000-000000000099")
NOW = datetime(2026, 8, 4, 16, 0, tzinfo=UTC)
POLICY_SENTENCE = "显示签收但客户否认收货时，必须先查询签收证明。"

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        TEST_DATABASE_URL is None,
        reason="TEST_DATABASE_URL is not configured",
    ),
]


def _vector() -> tuple[float, ...]:
    values = [0.0] * KNOWLEDGE_EMBEDDING_DIMENSIONS
    values[0] = 1.0
    return tuple(values)


class AnswerFixtureEmbeddingProvider:
    dimensions = KNOWLEDGE_EMBEDDING_DIMENSIONS

    def __init__(self) -> None:
        self.model = f"day14-postgres-{uuid4().hex[:12]}"

    def embed(self, texts):
        return tuple(_vector() for _ in texts)


def _database() -> Database:
    assert TEST_DATABASE_URL is not None
    database_name = urlparse(TEST_DATABASE_URL).path.removeprefix("/")
    assert database_name.endswith("_test")
    upgrade_database(TEST_DATABASE_URL)
    return Database(TEST_DATABASE_URL)


def _raw(output: object) -> RawProviderResponse:
    return RawProviderResponse(
        output=output,
        model="day14-grounding-fixture",
        input_tokens=50,
        output_tokens=20,
    )


def test_grounded_answer_trace_round_trip_on_real_postgres(
    request: pytest.FixtureRequest,
) -> None:
    database = _database()
    request.addfinalizer(database.dispose)
    knowledge_repository = SqlAlchemyKnowledgeRepository(
        database.session_factory
    )
    search_repository = SqlAlchemyKnowledgeSearchRepository(
        database.session_factory
    )
    unique = uuid4()
    ingestion = KnowledgeIngestionService(
        knowledge_repository,
        clock=lambda: NOW,
    ).ingest(
        IngestPolicyDocumentCommand(
            tenant_id=TENANT_ID,
            source_key=f"policy/day14/grounded-{unique}",
            document_version="1.0.0",
            title="签收争议处理政策",
            source_uri=f"repo://policies/day14-{unique}.md",
            source_text=f"# 签收争议处理政策\n\n{POLICY_SENTENCE}",
            effective_from=NOW - timedelta(days=1),
            effective_to=NOW + timedelta(days=1),
            allowed_roles=(Role.AGENT,),
            idempotency_key=f"day14-ingest-{unique}",
            request_id=f"day14-ingest-request-{unique}",
            trace_id=f"day14-ingest-trace-{unique}",
        )
    )
    assert ingestion.document is not None
    embedding_provider = AnswerFixtureEmbeddingProvider()
    KnowledgeIndexingService(
        knowledge_repository,
        search_repository,
        embedding_provider,
        clock=lambda: NOW,
    ).index_document(TENANT_ID, ingestion.document.id)

    provider = FakeLLMProvider(
        [
            _raw(
                {
                    "assessments": [
                        {
                            "evidence_id": "K1",
                            "relevance": 98,
                            "relation": "direct",
                        }
                    ],
                    "conflicts": [],
                }
            ),
            _raw(
                {
                    "disposition": "answered",
                    "claims": [
                        {
                            "text": "应先查询签收证明。",
                            "supports": [
                                {
                                    "evidence_id": "K1",
                                    "exact_quote": POLICY_SENTENCE,
                                }
                            ],
                        }
                    ],
                }
            ),
        ]
    )
    model_gateway = ModelGateway(
        provider,
        SqlAlchemyModelCallRepository(database.session_factory),
        model="day14-grounding-fixture",
        reasoning_effort="low",
        max_output_tokens=800,
        max_attempts=1,
        retry_base_seconds=1,
        wall_clock=lambda: NOW,
        monotonic_clock=lambda: 1.0,
    )
    trace_id = f"day14-answer-trace-{unique}"
    result = GroundedAnswerService(
        KnowledgeSearchService(
            search_repository,
            embedding_provider,
            wall_clock=lambda: NOW,
            monotonic_clock=lambda: 1.0,
        ),
        model_gateway,
        SqlAlchemyKnowledgeAnswerRepository(database.session_factory),
        wall_clock=lambda: NOW,
        monotonic_clock=lambda: 1.0,
    ).answer(
        AuthenticatedActor(
            actor_id="day14-postgres-agent",
            tenant_id=TENANT_ID,
            roles=frozenset({Role.AGENT}),
        ),
        GroundedAnswerQuery(
            question="签收后客户说没有收到，下一步做什么？",
            as_of=NOW,
            retrieval_mode=RetrievalMode.SEMANTIC,
            candidate_k=5,
            max_evidence=3,
            request_id=f"day14-answer-request-{unique}",
            trace_id=trace_id,
        ),
    )

    assert result.status is KnowledgeAnswerStatus.ANSWERED
    assert result.answer_text == "应先查询签收证明。 [K1]"
    assert result.citations[0].exact_quote == POLICY_SENTENCE
    with database.session_factory() as session:
        stored_run = session.get(KnowledgeAnswerRunRecord, result.run.id)
        stored_citation = session.get(
            KnowledgeAnswerCitationRecord,
            (result.run.id, "K1"),
        )
        stored_retrieval = session.get(
            KnowledgeRetrievalRunRecord,
            result.run.retrieval_run_id,
        )
        model_calls = session.scalars(
            select(ModelCallRecordModel)
            .where(ModelCallRecordModel.trace_id == trace_id)
            .order_by(ModelCallRecordModel.operation)
        ).all()
    assert stored_run is not None
    assert stored_run.status == KnowledgeAnswerStatus.ANSWERED.value
    assert stored_run.citation_count == 1
    assert stored_run.query_hash != result.answer_text
    assert stored_citation is not None
    assert stored_citation.quote_hash != POLICY_SENTENCE
    assert stored_citation.claim_indexes == [1]
    assert stored_retrieval is not None
    assert {call.operation for call in model_calls} == {
        "knowledge_evidence_rerank",
        "knowledge_grounded_answer",
    }
    assert {call.resource_id for call in model_calls} == {str(result.run.id)}
