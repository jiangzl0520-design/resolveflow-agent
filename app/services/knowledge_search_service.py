from datetime import UTC, datetime
from hashlib import sha256
import json
from time import perf_counter
from typing import Callable
from uuid import uuid4

from opentelemetry.trace import SpanKind

from app.core.errors import AuthorizationDeniedError
from app.domain.auth import AuthenticatedActor, Permission
from app.domain.retrieval import (
    KnowledgeRetrievalCandidate,
    KnowledgeRetrievalHitTrace,
    KnowledgeRetrievalRun,
    KnowledgeSearchHit,
    KnowledgeSearchQuery,
    KnowledgeSearchResult,
    RetrievalMode,
)
from app.knowledge.constants import (
    DEFAULT_RRF_K,
    KNOWLEDGE_EMBEDDING_DIMENSIONS,
)
from app.knowledge.embeddings import EmbeddingProvider
from app.knowledge.query_rewrite import (
    KnowledgeQueryRewriter,
    RuleBasedKnowledgeQueryRewriter,
)
from app.knowledge.retrieval_errors import (
    KnowledgeEmbeddingProviderError,
    KnowledgeEmbeddingContractError,
    KnowledgeRetrievalError,
    KnowledgeRetrievalRecordingError,
    KnowledgeSearchStorageError,
)
from app.repositories.knowledge_search_repository import (
    KnowledgeSearchRepository,
)
from app.observability.tracing import (
    FailureDomain,
    add_safe_event,
    mark_span_error,
    operation_span,
    set_safe_attributes,
)
from app.observability.metrics import get_metrics


class KnowledgeSearchService:
    def __init__(
        self,
        repository: KnowledgeSearchRepository,
        embedding_provider: EmbeddingProvider,
        *,
        query_rewriter: KnowledgeQueryRewriter | None = None,
        rrf_k: int = DEFAULT_RRF_K,
        semantic_weight: float = 1.0,
        keyword_weight: float = 1.0,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic_clock: Callable[[], float] = perf_counter,
    ) -> None:
        if rrf_k < 1:
            raise ValueError("rrf_k must be positive.")
        if semantic_weight <= 0 or keyword_weight <= 0:
            raise ValueError("RRF channel weights must be positive.")
        if embedding_provider.dimensions != KNOWLEDGE_EMBEDDING_DIMENSIONS:
            raise ValueError("Knowledge embedding dimensions do not match storage.")
        self._repository = repository
        self._embedding_provider = embedding_provider
        self._query_rewriter = (
            query_rewriter or RuleBasedKnowledgeQueryRewriter()
        )
        self._rrf_k = rrf_k
        self._semantic_weight = semantic_weight
        self._keyword_weight = keyword_weight
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock

    def search(
        self,
        actor: AuthenticatedActor,
        query: KnowledgeSearchQuery,
    ) -> KnowledgeSearchResult:
        metrics_started = self._monotonic_clock()
        with operation_span(
            "retrieve knowledge",
            kind=SpanKind.CLIENT,
            failure_domain=FailureDomain.RETRIEVAL,
            attributes={
                "resolveflow.component": "retrieval",
                "resolveflow.retrieval.mode": query.mode.value,
                "resolveflow.retrieval.top_k": query.top_k,
                "resolveflow.retrieval.embedding_model": (
                    self._embedding_provider.model
                ),
                "resolveflow.retrieval.query_hash": _hash_text(query.text),
                "resolveflow.request_id": query.request_id,
                "resolveflow.trace_id": query.trace_id,
            },
        ) as span:
            try:
                result = self._search(actor, query)
            except AuthorizationDeniedError as exc:
                get_metrics().record_retrieval(
                    mode=query.mode.value, status="failed",
                    error_code="authorization_denied",
                    duration=self._monotonic_clock() - metrics_started,
                )
                mark_span_error(
                    span,
                    FailureDomain.SECURITY,
                    type(exc).__name__,
                    retryable=False,
                )
                raise
            except KnowledgeRetrievalError as exc:
                get_metrics().record_retrieval(
                    mode=query.mode.value, status="failed", error_code=exc.code,
                    duration=self._monotonic_clock() - metrics_started,
                )
                mark_span_error(
                    span,
                    FailureDomain.RETRIEVAL,
                    exc.code,
                    retryable=exc.retryable,
                )
                raise
            set_safe_attributes(
                span,
                {
                    "resolveflow.retrieval.run_id": result.run.id,
                    "resolveflow.retrieval.semantic_candidates": (
                        result.run.semantic_candidate_count
                    ),
                    "resolveflow.retrieval.keyword_candidates": (
                        result.run.keyword_candidate_count
                    ),
                    "resolveflow.retrieval.result_count": (
                        result.run.result_count
                    ),
                    "resolveflow.retrieval.latency_ms": result.run.latency_ms,
                    "resolveflow.retrieval.degraded_reason": (
                        result.run.degraded_reason
                    ),
                },
            )
            get_metrics().record_retrieval(
                mode=query.mode.value,
                status="degraded" if result.run.degraded_reason else "succeeded",
                error_code=(result.run.degraded_reason.split(";")[0]
                            if result.run.degraded_reason else None),
                duration=result.run.latency_ms / 1000,
            )
            if result.run.degraded_reason:
                add_safe_event(
                    span,
                    "retrieval.degraded",
                    {"reason": result.run.degraded_reason},
                )
            return result

    def _search(
        self,
        actor: AuthenticatedActor,
        query: KnowledgeSearchQuery,
    ) -> KnowledgeSearchResult:
        if not actor.has_permission(Permission.TOOL_POLICY_READ):
            raise AuthorizationDeniedError(Permission.TOOL_POLICY_READ.value)
        started_tick = self._monotonic_clock()
        rewritten = self._query_rewriter.rewrite(query.text)
        candidate_limit = min(80, max(20, query.top_k * 4))
        semantic: list[KnowledgeRetrievalCandidate] = []
        keyword: list[KnowledgeRetrievalCandidate] = []
        degraded: list[str] = []
        channel_errors: list[KnowledgeRetrievalError] = []
        semantic_succeeded = query.mode is RetrievalMode.KEYWORD
        keyword_succeeded = query.mode is RetrievalMode.SEMANTIC

        if query.mode in {RetrievalMode.SEMANTIC, RetrievalMode.HYBRID}:
            try:
                query_vectors = self._embedding_provider.embed(
                    [rewritten.semantic_text]
                )
                if len(query_vectors) != 1:
                    raise KnowledgeEmbeddingProviderError(
                        "knowledge_embedding_contract_error",
                        "Knowledge query embedding was invalid.",
                        retryable=False,
                    )
                semantic = self._repository.semantic_search(
                    tenant_id=actor.tenant_id,
                    actor_roles=actor.roles,
                    as_of=query.as_of,
                    embedding=query_vectors[0],
                    embedding_model=self._embedding_provider.model,
                    limit=candidate_limit,
                )
                semantic_succeeded = True
            except (
                KnowledgeEmbeddingProviderError,
                KnowledgeEmbeddingContractError,
                KnowledgeSearchStorageError,
            ) as exc:
                channel_errors.append(exc)
                degraded.append(exc.code)

        if query.mode in {RetrievalMode.KEYWORD, RetrievalMode.HYBRID}:
            try:
                keyword = self._repository.keyword_search(
                    tenant_id=actor.tenant_id,
                    actor_roles=actor.roles,
                    as_of=query.as_of,
                    rewritten=rewritten,
                    limit=candidate_limit,
                )
                keyword_succeeded = True
            except KnowledgeSearchStorageError as exc:
                channel_errors.append(exc)
                degraded.append(exc.code)

        if not semantic_succeeded and not keyword_succeeded:
            if channel_errors:
                raise channel_errors[0]
            raise KnowledgeSearchStorageError()
        if query.mode is RetrievalMode.SEMANTIC and not semantic_succeeded:
            raise channel_errors[0]
        if query.mode is RetrievalMode.KEYWORD and not keyword_succeeded:
            raise channel_errors[0]

        hits = _reciprocal_rank_fusion(
            semantic,
            keyword,
            top_k=query.top_k,
            rrf_k=self._rrf_k,
            semantic_weight=self._semantic_weight,
            keyword_weight=self._keyword_weight,
        )
        run_id = uuid4()
        run = KnowledgeRetrievalRun(
            id=run_id,
            tenant_id=actor.tenant_id,
            actor_id=actor.actor_id,
            actor_roles=tuple(sorted(actor.roles, key=lambda role: role.value)),
            query_hash=_hash_text(query.text),
            rewritten_query_hash=_hash_rewrite(rewritten),
            rewrite_strategy=rewritten.strategy,
            search_mode=query.mode,
            embedding_model=self._embedding_provider.model,
            top_k=query.top_k,
            semantic_candidate_count=len(semantic),
            keyword_candidate_count=len(keyword),
            result_count=len(hits),
            degraded_reason=";".join(degraded) if degraded else None,
            latency_ms=(self._monotonic_clock() - started_tick) * 1000,
            request_id=query.request_id,
            trace_id=query.trace_id,
            created_at=self._wall_clock(),
        )
        traces = tuple(
            KnowledgeRetrievalHitTrace(
                run_id=run_id,
                rank=rank,
                chunk_id=hit.chunk_id,
                semantic_rank=hit.semantic_rank,
                keyword_rank=hit.keyword_rank,
                fused_score=hit.fused_score,
                vector_distance=hit.vector_distance,
                keyword_score=hit.keyword_score,
            )
            for rank, hit in enumerate(hits, start=1)
        )
        try:
            self._repository.record_retrieval(run, traces)
        except Exception as exc:
            raise KnowledgeRetrievalRecordingError() from exc
        return KnowledgeSearchResult(
            run=run,
            rewritten_query=rewritten,
            hits=tuple(hits),
        )


def _reciprocal_rank_fusion(
    semantic: list[KnowledgeRetrievalCandidate],
    keyword: list[KnowledgeRetrievalCandidate],
    *,
    top_k: int,
    rrf_k: int,
    semantic_weight: float,
    keyword_weight: float,
) -> list[KnowledgeSearchHit]:
    candidates: dict[str, KnowledgeRetrievalCandidate] = {}
    semantic_ranks: dict[str, int] = {}
    keyword_ranks: dict[str, int] = {}
    scores: dict[str, float] = {}
    for rank, candidate in enumerate(semantic, start=1):
        key = str(candidate.chunk_id)
        candidates[key] = candidate
        semantic_ranks[key] = rank
        scores[key] = scores.get(key, 0.0) + semantic_weight / (
            rrf_k + rank
        )
    for rank, candidate in enumerate(keyword, start=1):
        key = str(candidate.chunk_id)
        existing = candidates.get(key)
        if existing is None:
            candidates[key] = candidate
        else:
            candidates[key] = existing.model_copy(
                update={"keyword_score": candidate.keyword_score}
            )
        keyword_ranks[key] = rank
        scores[key] = scores.get(key, 0.0) + keyword_weight / (
            rrf_k + rank
        )
    ordered = sorted(
        candidates,
        key=lambda key: (
            -scores[key],
            min(
                semantic_ranks.get(key, 1_000_000),
                keyword_ranks.get(key, 1_000_000),
            ),
            key,
        ),
    )
    return [
        _to_hit(
            candidates[key],
            fused_score=scores[key],
            semantic_rank=semantic_ranks.get(key),
            keyword_rank=keyword_ranks.get(key),
        )
        for key in ordered[:top_k]
    ]


def _to_hit(
    candidate: KnowledgeRetrievalCandidate,
    *,
    fused_score: float,
    semantic_rank: int | None,
    keyword_rank: int | None,
) -> KnowledgeSearchHit:
    return KnowledgeSearchHit(
        chunk_id=candidate.chunk_id,
        document_id=candidate.document_id,
        source_key=candidate.source_key,
        title=candidate.title,
        content=candidate.content,
        source_uri=candidate.source_uri,
        document_version=candidate.document_version,
        source_line_start=candidate.source_line_start,
        source_line_end=candidate.source_line_end,
        effective_from=candidate.effective_from,
        effective_to=candidate.effective_to,
        fused_score=fused_score,
        semantic_rank=semantic_rank,
        keyword_rank=keyword_rank,
        vector_distance=candidate.vector_distance,
        keyword_score=candidate.keyword_score,
    )


def _hash_text(value: str) -> str:
    normalized = " ".join(value.split()).casefold()
    return sha256(normalized.encode("utf-8")).hexdigest()


def _hash_rewrite(rewritten) -> str:
    serialized = json.dumps(
        {
            "semantic_text": rewritten.semantic_text,
            "keyword_terms": rewritten.keyword_terms,
            "strategy": rewritten.strategy,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return sha256(serialized.encode("utf-8")).hexdigest()
