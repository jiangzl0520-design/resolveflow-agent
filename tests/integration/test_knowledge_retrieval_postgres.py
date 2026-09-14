from datetime import UTC, datetime, timedelta
from os import environ
from urllib.parse import urlparse
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text

from app.db.database import Database
from app.db.models import (
    KnowledgeRetrievalHitRecord,
    KnowledgeRetrievalRunRecord,
)
from app.domain.auth import AuthenticatedActor, Role
from app.domain.knowledge import IngestPolicyDocumentCommand
from app.domain.retrieval import KnowledgeSearchQuery, RetrievalMode
from app.knowledge.constants import KNOWLEDGE_EMBEDDING_DIMENSIONS
from app.repositories.sqlalchemy_knowledge_repository import (
    SqlAlchemyKnowledgeRepository,
)
from app.repositories.sqlalchemy_knowledge_search_repository import (
    SqlAlchemyKnowledgeSearchRepository,
)
from app.services.knowledge_indexing_service import KnowledgeIndexingService
from app.services.knowledge_ingestion_service import KnowledgeIngestionService
from app.services.knowledge_search_service import KnowledgeSearchService
from tests.integration.database_helpers import upgrade_database

TEST_DATABASE_URL = environ.get("TEST_DATABASE_URL")
TENANT_ID = UUID("83000000-0000-0000-0000-000000000001")
OTHER_TENANT_ID = UUID("83000000-0000-0000-0000-000000000002")
NOW = datetime(2026, 8, 4, 13, 0, tzinfo=UTC)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        TEST_DATABASE_URL is None,
        reason="TEST_DATABASE_URL is not configured",
    ),
]


def vector(position: int) -> tuple[float, ...]:
    values = [0.0] * KNOWLEDGE_EMBEDDING_DIMENSIONS
    values[position] = 1.0
    return tuple(values)


class SemanticFixtureProvider:
    dimensions = KNOWLEDGE_EMBEDDING_DIMENSIONS

    def __init__(self) -> None:
        self.model = f"postgres-fixture-{uuid4().hex[:12]}"

    def embed(self, texts):
        return tuple(
            vector(0)
            if any(term in text for term in ("妥投争议", "标记送达"))
            else vector(1)
            for text in texts
        )


def database() -> Database:
    assert TEST_DATABASE_URL is not None
    name = urlparse(TEST_DATABASE_URL).path.removeprefix("/")
    assert name.endswith("_test")
    upgrade_database(TEST_DATABASE_URL)
    return Database(TEST_DATABASE_URL)


def actor(
    *,
    tenant_id: UUID = TENANT_ID,
    role: Role = Role.AGENT,
) -> AuthenticatedActor:
    return AuthenticatedActor(
        actor_id=f"postgres-{role.value}",
        tenant_id=tenant_id,
        roles=frozenset({role}),
    )


def ingest(
    knowledge: SqlAlchemyKnowledgeRepository,
    *,
    source_key: str,
    text: str,
    tenant_id: UUID = TENANT_ID,
    roles: tuple[Role, ...] = (Role.AGENT, Role.SUPERVISOR),
    effective_from: datetime = NOW - timedelta(days=1),
    effective_to: datetime | None = NOW + timedelta(days=1),
):
    unique = uuid4()
    return KnowledgeIngestionService(knowledge, clock=lambda: NOW).ingest(
        IngestPolicyDocumentCommand(
            tenant_id=tenant_id,
            source_key=f"{source_key}/{unique}",
            document_version="1.0.0",
            title=f"PostgreSQL检索政策{unique}",
            source_uri=f"repo://policies/{unique}.md",
            source_text=f"# PostgreSQL检索政策\n\n## 规则\n\n{text}",
            effective_from=effective_from,
            effective_to=effective_to,
            allowed_roles=roles,
            idempotency_key=f"day13-postgres-{unique}",
            request_id=f"day13-request-{unique}",
            trace_id=f"day13-trace-{unique}",
        )
    )


def search_query(
    text: str,
    *,
    mode: RetrievalMode = RetrievalMode.HYBRID,
) -> KnowledgeSearchQuery:
    unique = uuid4()
    return KnowledgeSearchQuery(
        text=text,
        as_of=NOW,
        top_k=10,
        mode=mode,
        request_id=f"day13-search-request-{unique}",
        trace_id=f"day13-search-trace-{unique}",
    )


def test_real_pgvector_semantic_search_and_trace_round_trip(
    request: pytest.FixtureRequest,
) -> None:
    db = database()
    request.addfinalizer(db.dispose)
    knowledge = SqlAlchemyKnowledgeRepository(db.session_factory)
    search_repository = SqlAlchemyKnowledgeSearchRepository(
        db.session_factory
    )
    target = ingest(
        knowledge,
        source_key="policy/day13/semantic-target",
        text="妥投争议需要核对承运方留存的交付佐证。",
    )
    distractor = ingest(
        knowledge,
        source_key="policy/day13/semantic-distractor",
        text="未付款订单会自动取消，不进入售后退款流程。",
    )
    assert target.document is not None
    assert distractor.document is not None
    provider = SemanticFixtureProvider()
    indexer = KnowledgeIndexingService(
        knowledge,
        search_repository,
        provider,
        clock=lambda: NOW,
    )
    indexer.index_document(TENANT_ID, target.document.id)
    indexer.index_document(TENANT_ID, distractor.document.id)

    result = KnowledgeSearchService(
        search_repository,
        provider,
        wall_clock=lambda: NOW,
    ).search(
        actor(),
        search_query("包裹被标记送达但客户否认", mode=RetrievalMode.SEMANTIC),
    )

    assert result.hits
    assert result.hits[0].document_id == target.document.id
    assert result.hits[0].vector_distance == pytest.approx(0.0)
    with db.session_factory() as session:
        extension_version = session.scalar(
            text(
                "SELECT extversion FROM pg_extension "
                "WHERE extname = 'vector'"
            )
        )
        stored_run = session.get(KnowledgeRetrievalRunRecord, result.run.id)
        stored_hits = session.scalars(
            select(KnowledgeRetrievalHitRecord).where(
                KnowledgeRetrievalHitRecord.run_id == result.run.id
            )
        ).all()
    assert extension_version is not None
    assert stored_run is not None
    assert stored_run.query_hash == result.run.query_hash
    assert len(stored_hits) == len(result.hits)


def test_postgres_keyword_search_filters_tenant_acl_and_effective_time(
    request: pytest.FixtureRequest,
) -> None:
    db = database()
    request.addfinalizer(db.dispose)
    knowledge = SqlAlchemyKnowledgeRepository(db.session_factory)
    repository = SqlAlchemyKnowledgeSearchRepository(db.session_factory)
    marker = f"D13{uuid4().hex[:20].upper()}"
    visible = ingest(
        knowledge,
        source_key="policy/day13/visible",
        text=f"{marker} 签收证明需要核验。",
    )
    restricted = ingest(
        knowledge,
        source_key="policy/day13/restricted",
        text=f"{marker} 仅主管可见。",
        roles=(Role.SUPERVISOR,),
    )
    ingest(
        knowledge,
        source_key="policy/day13/other-tenant",
        text=f"{marker} 其他租户政策。",
        tenant_id=OTHER_TENANT_ID,
    )
    ingest(
        knowledge,
        source_key="policy/day13/expired",
        text=f"{marker} 已过期政策。",
        effective_to=NOW,
    )
    ingest(
        knowledge,
        source_key="policy/day13/future",
        text=f"{marker} 未来政策。",
        effective_from=NOW + timedelta(seconds=1),
        effective_to=None,
    )
    service = KnowledgeSearchService(repository, SemanticFixtureProvider())

    agent_result = service.search(
        actor(),
        search_query(marker, mode=RetrievalMode.KEYWORD),
    )
    supervisor_result = service.search(
        actor(role=Role.SUPERVISOR),
        search_query(marker, mode=RetrievalMode.KEYWORD),
    )
    other_result = service.search(
        actor(tenant_id=OTHER_TENANT_ID),
        search_query(marker, mode=RetrievalMode.KEYWORD),
    )

    assert visible.document is not None
    assert restricted.document is not None
    assert {hit.document_id for hit in agent_result.hits} == {
        visible.document.id
    }
    assert {hit.document_id for hit in supervisor_result.hits} == {
        visible.document.id,
        restricted.document.id,
    }
    assert len(other_result.hits) == 1
