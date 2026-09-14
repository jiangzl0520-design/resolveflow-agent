from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    case,
    cast,
    func,
    literal,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from pgvector.sqlalchemy import VECTOR

from app.db.models import (
    KnowledgeChunkRecord,
    KnowledgeDocumentRecord,
    KnowledgeRetrievalHitRecord,
    KnowledgeRetrievalRunRecord,
)
from app.domain.auth import Role
from app.domain.retrieval import (
    ChunkEmbeddingUpdate,
    KnowledgeRetrievalCandidate,
    KnowledgeRetrievalHitTrace,
    KnowledgeRetrievalRun,
    RewrittenKnowledgeQuery,
)
from app.knowledge.retrieval_errors import KnowledgeSearchStorageError
from app.knowledge.constants import (
    DEFAULT_MAX_SEMANTIC_DISTANCE,
    KNOWLEDGE_EMBEDDING_DIMENSIONS,
)


class SqlAlchemyKnowledgeSearchRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def store_embeddings(
        self,
        tenant_id: UUID,
        updates: tuple[ChunkEmbeddingUpdate, ...],
    ) -> None:
        with self._session_factory() as session:
            try:
                for item in updates:
                    result = session.execute(
                        update(KnowledgeChunkRecord)
                        .where(
                            KnowledgeChunkRecord.id == item.chunk_id,
                            KnowledgeChunkRecord.tenant_id == tenant_id,
                            KnowledgeChunkRecord.content_hash
                            == item.content_hash,
                        )
                        .values(
                            embedding=list(item.embedding),
                            embedding_model=item.embedding_model,
                            embedded_at=item.embedded_at,
                        )
                    )
                    if result.rowcount != 1:
                        raise KnowledgeSearchStorageError()
                session.commit()
            except KnowledgeSearchStorageError:
                session.rollback()
                raise
            except (IntegrityError, SQLAlchemyError, ValueError) as exc:
                session.rollback()
                raise KnowledgeSearchStorageError() from exc

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
        distance = cast(
            KnowledgeChunkRecord.embedding,
            VECTOR(KNOWLEDGE_EMBEDDING_DIMENSIONS),
        ).cosine_distance(list(embedding)).label("vector_distance")
        statement = (
            select(
                KnowledgeChunkRecord,
                KnowledgeDocumentRecord.source_key,
                KnowledgeDocumentRecord.title,
                distance,
            )
            .join(
                KnowledgeDocumentRecord,
                KnowledgeDocumentRecord.id
                == KnowledgeChunkRecord.document_id,
            )
            .where(
                *_visibility_filters(
                    tenant_id=tenant_id,
                    actor_roles=actor_roles,
                    as_of=as_of,
                ),
                KnowledgeChunkRecord.embedding.is_not(None),
                KnowledgeChunkRecord.embedding_model == embedding_model,
                distance <= DEFAULT_MAX_SEMANTIC_DISTANCE,
            )
            .order_by(distance, KnowledgeChunkRecord.id)
            .limit(limit)
        )
        return self._execute_candidates(statement, semantic=True)

    def keyword_search(
        self,
        *,
        tenant_id: UUID,
        actor_roles: frozenset[Role],
        as_of: datetime,
        rewritten: RewrittenKnowledgeQuery,
        limit: int,
    ) -> list[KnowledgeRetrievalCandidate]:
        query_text = _websearch_query(rewritten)
        search_vector = func.to_tsvector("simple", KnowledgeChunkRecord.content)
        search_query = func.websearch_to_tsquery("simple", query_text)
        full_text_score = func.ts_rank_cd(search_vector, search_query)
        exact_scores = [
            case(
                (
                    KnowledgeChunkRecord.content.ilike(
                        f"%{_escape_like(term)}%",
                        escape="\\",
                    ),
                    1.0,
                ),
                else_=0.0,
            )
            for term in rewritten.keyword_terms
        ]
        exact_score = (
            sum(exact_scores, start=literal(0.0))
            if exact_scores
            else literal(0.0)
        )
        trigram_score = func.similarity(
            KnowledgeChunkRecord.content,
            rewritten.semantic_text,
        )
        keyword_score = (
            full_text_score + exact_score + trigram_score * 0.25
        ).label("keyword_score")
        exact_conditions = [
            KnowledgeChunkRecord.content.ilike(
                f"%{_escape_like(term)}%",
                escape="\\",
            )
            for term in rewritten.keyword_terms
        ]
        exact_matches = (
            or_(*exact_conditions)
            if exact_conditions
            else literal(False)
        )
        match_condition = or_(
            search_vector.op("@@")(search_query),
            exact_matches,
            trigram_score >= 0.08,
        )
        statement = (
            select(
                KnowledgeChunkRecord,
                KnowledgeDocumentRecord.source_key,
                KnowledgeDocumentRecord.title,
                keyword_score,
            )
            .join(
                KnowledgeDocumentRecord,
                KnowledgeDocumentRecord.id
                == KnowledgeChunkRecord.document_id,
            )
            .where(
                *_visibility_filters(
                    tenant_id=tenant_id,
                    actor_roles=actor_roles,
                    as_of=as_of,
                ),
                match_condition,
            )
            .order_by(keyword_score.desc(), KnowledgeChunkRecord.id)
            .limit(limit)
        )
        return self._execute_candidates(statement, semantic=False)

    def record_retrieval(
        self,
        run: KnowledgeRetrievalRun,
        hits: tuple[KnowledgeRetrievalHitTrace, ...],
    ) -> None:
        with self._session_factory() as session:
            try:
                session.add(
                    KnowledgeRetrievalRunRecord(
                        id=run.id,
                        tenant_id=run.tenant_id,
                        actor_id=run.actor_id,
                        actor_roles=[role.value for role in run.actor_roles],
                        query_hash=run.query_hash,
                        rewritten_query_hash=run.rewritten_query_hash,
                        rewrite_strategy=run.rewrite_strategy,
                        search_mode=run.search_mode.value,
                        embedding_model=run.embedding_model,
                        top_k=run.top_k,
                        semantic_candidate_count=(
                            run.semantic_candidate_count
                        ),
                        keyword_candidate_count=run.keyword_candidate_count,
                        result_count=run.result_count,
                        degraded_reason=run.degraded_reason,
                        latency_ms=run.latency_ms,
                        request_id=run.request_id,
                        trace_id=run.trace_id,
                        created_at=run.created_at,
                    )
                )
                session.flush()
                session.add_all(
                    KnowledgeRetrievalHitRecord(
                        run_id=hit.run_id,
                        rank=hit.rank,
                        chunk_id=hit.chunk_id,
                        semantic_rank=hit.semantic_rank,
                        keyword_rank=hit.keyword_rank,
                        fused_score=hit.fused_score,
                        vector_distance=hit.vector_distance,
                        keyword_score=hit.keyword_score,
                    )
                    for hit in hits
                )
                session.commit()
            except (IntegrityError, SQLAlchemyError) as exc:
                session.rollback()
                raise KnowledgeSearchStorageError() from exc

    def _execute_candidates(
        self,
        statement,
        *,
        semantic: bool,
    ) -> list[KnowledgeRetrievalCandidate]:
        with self._session_factory() as session:
            try:
                rows = session.execute(statement).all()
            except (SQLAlchemyError, ValueError) as exc:
                raise KnowledgeSearchStorageError() from exc
        return [
            _to_candidate(
                record,
                source_key=source_key,
                title=title,
                vector_distance=float(score) if semantic else None,
                keyword_score=float(score) if not semantic else None,
            )
            for record, source_key, title, score in rows
        ]


def _visibility_filters(
    *,
    tenant_id: UUID,
    actor_roles: frozenset[Role],
    as_of: datetime,
) -> tuple:
    role_filter = or_(
        *[
            KnowledgeChunkRecord.allowed_roles.op("?")(role.value)
            for role in sorted(actor_roles, key=lambda item: item.value)
        ]
    )
    return (
        KnowledgeChunkRecord.tenant_id == tenant_id,
        role_filter,
        KnowledgeChunkRecord.effective_from <= as_of,
        or_(
            KnowledgeChunkRecord.effective_to.is_(None),
            as_of < KnowledgeChunkRecord.effective_to,
        ),
    )


def _to_candidate(
    record: KnowledgeChunkRecord,
    *,
    source_key: str,
    title: str,
    vector_distance: float | None,
    keyword_score: float | None,
) -> KnowledgeRetrievalCandidate:
    return KnowledgeRetrievalCandidate(
        chunk_id=record.id,
        document_id=record.document_id,
        tenant_id=record.tenant_id,
        source_key=source_key,
        title=title,
        content=record.content,
        source_uri=record.source_uri,
        document_version=record.document_version,
        source_line_start=record.source_line_start,
        source_line_end=record.source_line_end,
        effective_from=record.effective_from,
        effective_to=record.effective_to,
        allowed_roles=tuple(Role(role) for role in record.allowed_roles),
        vector_distance=vector_distance,
        keyword_score=keyword_score,
    )


def _websearch_query(rewritten: RewrittenKnowledgeQuery) -> str:
    terms = rewritten.keyword_terms or (rewritten.semantic_text,)
    return " or ".join(f'"{term.replace(chr(34), " ")}"' for term in terms)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
