from datetime import datetime
from math import sqrt
from threading import RLock
from typing import Protocol
from uuid import UUID

from app.domain.auth import Role
from app.domain.retrieval import (
    ChunkEmbeddingUpdate,
    KnowledgeRetrievalCandidate,
    KnowledgeRetrievalHitTrace,
    KnowledgeRetrievalRun,
    RewrittenKnowledgeQuery,
)
from app.knowledge.constants import DEFAULT_MAX_SEMANTIC_DISTANCE


class KnowledgeEmbeddingRepository(Protocol):
    def store_embeddings(
        self,
        tenant_id: UUID,
        updates: tuple[ChunkEmbeddingUpdate, ...],
    ) -> None: ...


class KnowledgeSearchRepository(KnowledgeEmbeddingRepository, Protocol):
    def semantic_search(
        self,
        *,
        tenant_id: UUID,
        actor_roles: frozenset[Role],
        as_of: datetime,
        embedding: tuple[float, ...],
        embedding_model: str,
        limit: int,
    ) -> list[KnowledgeRetrievalCandidate]: ...

    def keyword_search(
        self,
        *,
        tenant_id: UUID,
        actor_roles: frozenset[Role],
        as_of: datetime,
        rewritten: RewrittenKnowledgeQuery,
        limit: int,
    ) -> list[KnowledgeRetrievalCandidate]: ...

    def record_retrieval(
        self,
        run: KnowledgeRetrievalRun,
        hits: tuple[KnowledgeRetrievalHitTrace, ...],
    ) -> None: ...


class InMemoryKnowledgeSearchRepository:
    def __init__(self) -> None:
        self._entries: dict[UUID, _Entry] = {}
        self.retrieval_runs: list[KnowledgeRetrievalRun] = []
        self.retrieval_hits: list[KnowledgeRetrievalHitTrace] = []
        self._lock = RLock()

    def add_entry(
        self,
        candidate: KnowledgeRetrievalCandidate,
        *,
        content_hash: str,
        embedding: tuple[float, ...] | None = None,
        embedding_model: str | None = None,
    ) -> None:
        with self._lock:
            self._entries[candidate.chunk_id] = _Entry(
                candidate=candidate,
                content_hash=content_hash,
                embedding=embedding,
                embedding_model=embedding_model,
            )

    def store_embeddings(
        self,
        tenant_id: UUID,
        updates: tuple[ChunkEmbeddingUpdate, ...],
    ) -> None:
        with self._lock:
            replacements: dict[UUID, _Entry] = {}
            for update in updates:
                entry = self._entries.get(update.chunk_id)
                if (
                    entry is None
                    or entry.candidate.tenant_id != tenant_id
                    or entry.content_hash != update.content_hash
                ):
                    raise ValueError("Knowledge embedding target changed.")
                replacements[update.chunk_id] = _Entry(
                    candidate=entry.candidate,
                    content_hash=entry.content_hash,
                    embedding=update.embedding,
                    embedding_model=update.embedding_model,
                )
            self._entries.update(replacements)

    def semantic_search(
        self,
        *,
        tenant_id: UUID,
        actor_roles: frozenset[Role],
        as_of: datetime,
        embedding: tuple[float, ...],
        embedding_model: str,
        limit: int,
    ) -> list[KnowledgeRetrievalCandidate]:
        ranked: list[tuple[float, KnowledgeRetrievalCandidate]] = []
        with self._lock:
            entries = list(self._entries.values())
        for entry in entries:
            if not _visible(entry.candidate, tenant_id, actor_roles, as_of):
                continue
            if entry.embedding is None or entry.embedding_model != embedding_model:
                continue
            distance = _cosine_distance(embedding, entry.embedding)
            if distance > DEFAULT_MAX_SEMANTIC_DISTANCE:
                continue
            ranked.append(
                (
                    distance,
                    entry.candidate.model_copy(
                        update={"vector_distance": distance}
                    ),
                )
            )
        ranked.sort(key=lambda item: (item[0], str(item[1].chunk_id)))
        return [candidate for _, candidate in ranked[:limit]]

    def keyword_search(
        self,
        *,
        tenant_id: UUID,
        actor_roles: frozenset[Role],
        as_of: datetime,
        rewritten: RewrittenKnowledgeQuery,
        limit: int,
    ) -> list[KnowledgeRetrievalCandidate]:
        ranked: list[tuple[float, KnowledgeRetrievalCandidate]] = []
        with self._lock:
            entries = list(self._entries.values())
        for entry in entries:
            candidate = entry.candidate
            if not _visible(candidate, tenant_id, actor_roles, as_of):
                continue
            haystack = candidate.content.casefold()
            score = sum(
                1.0 + haystack.count(term.casefold()) * 0.1
                for term in rewritten.keyword_terms
                if term.casefold() in haystack
            )
            if score <= 0:
                continue
            ranked.append(
                (
                    score,
                    candidate.model_copy(update={"keyword_score": score}),
                )
            )
        ranked.sort(key=lambda item: (-item[0], str(item[1].chunk_id)))
        return [candidate for _, candidate in ranked[:limit]]

    def record_retrieval(
        self,
        run: KnowledgeRetrievalRun,
        hits: tuple[KnowledgeRetrievalHitTrace, ...],
    ) -> None:
        with self._lock:
            self.retrieval_runs.append(run)
            self.retrieval_hits.extend(hits)


class _Entry:
    def __init__(
        self,
        *,
        candidate: KnowledgeRetrievalCandidate,
        content_hash: str,
        embedding: tuple[float, ...] | None,
        embedding_model: str | None,
    ) -> None:
        self.candidate = candidate
        self.content_hash = content_hash
        self.embedding = embedding
        self.embedding_model = embedding_model


def _visible(
    candidate: KnowledgeRetrievalCandidate,
    tenant_id: UUID,
    actor_roles: frozenset[Role],
    as_of: datetime,
) -> bool:
    return (
        candidate.tenant_id == tenant_id
        and bool(set(candidate.allowed_roles) & actor_roles)
        and candidate.effective_from <= as_of
        and (
            candidate.effective_to is None
            or as_of < candidate.effective_to
        )
    )


def _cosine_distance(
    left: tuple[float, ...],
    right: tuple[float, ...],
) -> float:
    if len(left) != len(right):
        raise ValueError("Embedding dimensions differ.")
    left_norm = sqrt(sum(value * value for value in left))
    right_norm = sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 1.0
    similarity = sum(a * b for a, b in zip(left, right, strict=True)) / (
        left_norm * right_norm
    )
    return 1.0 - similarity
