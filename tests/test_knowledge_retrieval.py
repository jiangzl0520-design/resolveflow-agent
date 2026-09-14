from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import pytest

from app.core.errors import AuthorizationDeniedError
from app.domain.auth import AuthenticatedActor, Role
from app.domain.knowledge import IngestPolicyDocumentCommand
from app.domain.retrieval import (
    KnowledgeRetrievalCandidate,
    KnowledgeSearchQuery,
    RetrievalMode,
)
from app.knowledge.constants import KNOWLEDGE_EMBEDDING_DIMENSIONS
from app.knowledge.query_rewrite import RuleBasedKnowledgeQueryRewriter
from app.knowledge.retrieval_errors import (
    KnowledgeEmbeddingProviderError,
    KnowledgeRetrievalRecordingError,
)
from app.repositories.knowledge_repository import InMemoryKnowledgeRepository
from app.repositories.knowledge_search_repository import (
    InMemoryKnowledgeSearchRepository,
)
from app.services.knowledge_indexing_service import KnowledgeIndexingService
from app.services.knowledge_ingestion_service import KnowledgeIngestionService
from app.services.knowledge_search_service import KnowledgeSearchService

TENANT_ID = UUID("82000000-0000-0000-0000-000000000001")
OTHER_TENANT_ID = UUID("82000000-0000-0000-0000-000000000002")
NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
AGENT = AuthenticatedActor(
    actor_id="retrieval-agent",
    tenant_id=TENANT_ID,
    roles=frozenset({Role.AGENT}),
)
SUPERVISOR = AuthenticatedActor(
    actor_id="retrieval-supervisor",
    tenant_id=TENANT_ID,
    roles=frozenset({Role.SUPERVISOR}),
)


def vector(position: int, secondary: int | None = None) -> tuple[float, ...]:
    values = [0.0] * KNOWLEDGE_EMBEDDING_DIMENSIONS
    values[position] = 1.0
    if secondary is not None:
        values[secondary] = 0.6
    return tuple(values)


class FixedEmbeddingProvider:
    model = "fixture-semantic-v1"
    dimensions = KNOWLEDGE_EMBEDDING_DIMENSIONS

    def __init__(self, query_vector=None):
        self.query_vector = query_vector or vector(0)

    def embed(self, texts):
        return tuple(self.query_vector for _ in texts)


class FailingEmbeddingProvider(FixedEmbeddingProvider):
    def embed(self, texts):
        raise KnowledgeEmbeddingProviderError(
            "knowledge_embedding_timeout",
            "Synthetic embedding timeout.",
            retryable=True,
        )


def candidate(
    content: str,
    *,
    tenant_id: UUID = TENANT_ID,
    roles: tuple[Role, ...] = (Role.AGENT, Role.SUPERVISOR),
    effective_from: datetime = NOW - timedelta(days=1),
    effective_to: datetime | None = NOW + timedelta(days=1),
) -> KnowledgeRetrievalCandidate:
    chunk_id = uuid4()
    return KnowledgeRetrievalCandidate(
        chunk_id=chunk_id,
        document_id=uuid4(),
        tenant_id=tenant_id,
        source_key=f"policy/test/{chunk_id}",
        title="测试政策",
        content=content,
        source_uri=f"repo://policies/{chunk_id}.md",
        document_version="1.0.0",
        source_line_start=1,
        source_line_end=3,
        effective_from=effective_from,
        effective_to=effective_to,
        allowed_roles=roles,
    )


def add_entry(
    repository: InMemoryKnowledgeSearchRepository,
    item: KnowledgeRetrievalCandidate,
    embedding: tuple[float, ...],
) -> None:
    repository.add_entry(
        item,
        content_hash=sha256(item.content.encode()).hexdigest(),
        embedding=embedding,
        embedding_model="fixture-semantic-v1",
    )


def query(
    text: str,
    *,
    mode: RetrievalMode = RetrievalMode.HYBRID,
    top_k: int = 5,
) -> KnowledgeSearchQuery:
    return KnowledgeSearchQuery(
        text=text,
        as_of=NOW,
        mode=mode,
        top_k=top_k,
        request_id=f"request-{uuid4()}",
        trace_id=f"trace-{uuid4()}",
    )


def test_query_rewrite_removes_resource_id_and_normalizes_terms() -> None:
    rewritten = RuleBasedKnowledgeQueryRewriter().rewrite(
        "订单 10086 没收到包裹，想退钱"
    )

    assert "10086" not in rewritten.semantic_text
    assert "未收到货" in rewritten.semantic_text
    assert "退款" in rewritten.semantic_text
    assert rewritten.keyword_terms == ("未收到货", "退款")


def test_hybrid_rrf_rewards_candidate_found_by_both_channels() -> None:
    repository = InMemoryKnowledgeSearchRepository()
    semantic_only = candidate("异常妥投后的客户争议处理规则")
    keyword_only = candidate("签收证明 POLICY-X1 精确条款")
    both = candidate("签收证明的核验与争议处理")
    add_entry(repository, semantic_only, vector(0))
    add_entry(repository, keyword_only, vector(1))
    add_entry(repository, both, vector(0, 1))

    result = KnowledgeSearchService(
        repository,
        FixedEmbeddingProvider(vector(0)),
    ).search(AGENT, query("签收证明 POLICY-X1", top_k=3))

    assert result.hits[0].chunk_id == both.chunk_id
    assert result.hits[0].semantic_rank is not None
    assert result.hits[0].keyword_rank is not None
    assert result.run.semantic_candidate_count == 2
    assert result.run.keyword_candidate_count == 2
    assert repository.retrieval_runs == [result.run]
    assert len(repository.retrieval_hits) == 3


def test_acl_tenant_and_effective_time_filter_before_ranking() -> None:
    repository = InMemoryKnowledgeSearchRepository()
    visible = candidate("退款规则")
    restricted = candidate("退款规则", roles=(Role.SUPERVISOR,))
    other_tenant = candidate("退款规则", tenant_id=OTHER_TENANT_ID)
    expired = candidate("退款规则", effective_to=NOW)
    future = candidate("退款规则", effective_from=NOW + timedelta(seconds=1))
    for item in (visible, restricted, other_tenant, expired, future):
        add_entry(repository, item, vector(0))

    agent_result = KnowledgeSearchService(
        repository,
        FixedEmbeddingProvider(),
    ).search(AGENT, query("退款", mode=RetrievalMode.KEYWORD))
    supervisor_result = KnowledgeSearchService(
        repository,
        FixedEmbeddingProvider(),
    ).search(SUPERVISOR, query("退款", mode=RetrievalMode.KEYWORD))

    assert {hit.chunk_id for hit in agent_result.hits} == {visible.chunk_id}
    assert {hit.chunk_id for hit in supervisor_result.hits} == {
        visible.chunk_id,
        restricted.chunk_id,
    }


def test_customer_cannot_search_internal_policy() -> None:
    customer = AuthenticatedActor(
        actor_id="customer",
        tenant_id=TENANT_ID,
        roles=frozenset({Role.CUSTOMER}),
    )

    with pytest.raises(AuthorizationDeniedError):
        KnowledgeSearchService(
            InMemoryKnowledgeSearchRepository(),
            FixedEmbeddingProvider(),
        ).search(customer, query("退款"))


def test_hybrid_degrades_to_keyword_when_embedding_is_unavailable() -> None:
    repository = InMemoryKnowledgeSearchRepository()
    item = candidate("签收证明必须经过核验")
    add_entry(repository, item, vector(0))

    result = KnowledgeSearchService(
        repository,
        FailingEmbeddingProvider(),
    ).search(AGENT, query("签收证明"))

    assert [hit.chunk_id for hit in result.hits] == [item.chunk_id]
    assert result.run.semantic_candidate_count == 0
    assert result.run.keyword_candidate_count == 1
    assert result.run.degraded_reason == "knowledge_embedding_timeout"


def test_retrieval_recording_failure_blocks_untraced_result() -> None:
    class FailingRecorder(InMemoryKnowledgeSearchRepository):
        def record_retrieval(self, run, hits):
            raise RuntimeError("synthetic trace outage")

    repository = FailingRecorder()
    item = candidate("退款规则")
    add_entry(repository, item, vector(0))

    with pytest.raises(KnowledgeRetrievalRecordingError):
        KnowledgeSearchService(
            repository,
            FixedEmbeddingProvider(),
        ).search(AGENT, query("退款"))


def test_indexing_service_stores_embeddings_for_ingested_chunks() -> None:
    knowledge_repository = InMemoryKnowledgeRepository()
    ingestion = KnowledgeIngestionService(
        knowledge_repository,
        clock=lambda: NOW,
    )
    ingested = ingestion.ingest(
        IngestPolicyDocumentCommand(
            tenant_id=TENANT_ID,
            source_key="policy/test/indexing",
            document_version="1.0.0",
            title="索引测试政策",
            source_uri="repo://policies/indexing.md",
            source_text="# 索引测试政策\n\n## 退款\n\n退款需要审核。",
            effective_from=NOW - timedelta(days=1),
            idempotency_key="day13-indexing-test",
            request_id="day13-index-request",
            trace_id="day13-index-trace",
        )
    )
    assert ingested.document is not None
    search_repository = InMemoryKnowledgeSearchRepository()
    for chunk in ingested.chunks:
        search_repository.add_entry(
            candidate(
                chunk.content,
                roles=chunk.allowed_roles,
                effective_from=chunk.effective_from,
                effective_to=chunk.effective_to,
            ).model_copy(
                update={
                    "chunk_id": chunk.id,
                    "document_id": chunk.document_id,
                }
            ),
            content_hash=chunk.content_hash,
        )

    indexed = KnowledgeIndexingService(
        knowledge_repository,
        search_repository,
        FixedEmbeddingProvider(vector(0)),
        clock=lambda: NOW,
    ).index_document(TENANT_ID, ingested.document.id)
    result = KnowledgeSearchService(
        search_repository,
        FixedEmbeddingProvider(vector(0)),
    ).search(AGENT, query("费用返还规则", mode=RetrievalMode.SEMANTIC))

    assert indexed.indexed_chunk_count == len(ingested.chunks)
    assert {hit.chunk_id for hit in result.hits} == {
        chunk.id for chunk in ingested.chunks
    }
