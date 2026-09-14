from datetime import UTC, datetime
from math import isfinite
from typing import Callable
from uuid import UUID

from app.domain.retrieval import (
    ChunkEmbeddingUpdate,
    KnowledgeIndexingResult,
)
from app.knowledge.embeddings import EmbeddingProvider
from app.knowledge.constants import KNOWLEDGE_EMBEDDING_DIMENSIONS
from app.knowledge.retrieval_errors import (
    KnowledgeEmbeddingContractError,
    KnowledgeRetrievalError,
)
from app.repositories.knowledge_repository import KnowledgeRepository
from app.repositories.knowledge_search_repository import (
    KnowledgeEmbeddingRepository,
)


class KnowledgeIndexingService:
    def __init__(
        self,
        knowledge_repository: KnowledgeRepository,
        embedding_repository: KnowledgeEmbeddingRepository,
        embedding_provider: EmbeddingProvider,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if embedding_provider.dimensions != KNOWLEDGE_EMBEDDING_DIMENSIONS:
            raise ValueError("Knowledge embedding dimensions do not match storage.")
        self._knowledge_repository = knowledge_repository
        self._embedding_repository = embedding_repository
        self._embedding_provider = embedding_provider
        self._clock = clock

    def index_document(
        self,
        tenant_id: UUID,
        document_id: UUID,
    ) -> KnowledgeIndexingResult:
        document = self._knowledge_repository.get_document(
            tenant_id,
            document_id,
        )
        if document is None:
            raise KnowledgeRetrievalError(
                "knowledge_document_not_found",
                "Knowledge document was not found.",
                retryable=False,
            )
        chunks = self._knowledge_repository.list_chunks(
            tenant_id,
            document_id,
        )
        vectors = self._embedding_provider.embed(
            [chunk.content for chunk in chunks]
        )
        if len(vectors) != len(chunks):
            raise KnowledgeEmbeddingContractError()
        if any(
            len(vector) != self._embedding_provider.dimensions
            or any(not isfinite(value) for value in vector)
            for vector in vectors
        ):
            raise KnowledgeEmbeddingContractError()
        now = self._clock()
        updates = tuple(
            ChunkEmbeddingUpdate(
                chunk_id=chunk.id,
                content_hash=chunk.content_hash,
                embedding=vector,
                embedding_model=self._embedding_provider.model,
                embedded_at=now,
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        )
        self._embedding_repository.store_embeddings(tenant_id, updates)
        return KnowledgeIndexingResult(
            tenant_id=tenant_id,
            document_id=document_id,
            embedding_model=self._embedding_provider.model,
            indexed_chunk_count=len(updates),
        )
