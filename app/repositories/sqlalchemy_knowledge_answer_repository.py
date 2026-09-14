from sqlalchemy import update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    KnowledgeAnswerCitationRecord,
    KnowledgeAnswerRunRecord,
)
from app.domain.grounded_answer import (
    KnowledgeAnswerCitationTrace,
    KnowledgeAnswerRun,
    KnowledgeAnswerStatus,
)
from app.knowledge.answering_errors import KnowledgeAnswerRecordingError


class SqlAlchemyKnowledgeAnswerRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def add_run(self, run: KnowledgeAnswerRun) -> None:
        if run.status is not KnowledgeAnswerStatus.PROCESSING:
            raise ValueError("A new answer run must be processing.")
        with self._session_factory() as session:
            try:
                session.add(_to_record(run))
                session.commit()
            except (IntegrityError, SQLAlchemyError) as exc:
                session.rollback()
                raise KnowledgeAnswerRecordingError() from exc

    def finish_run(
        self,
        run: KnowledgeAnswerRun,
        citations: tuple[KnowledgeAnswerCitationTrace, ...],
    ) -> None:
        if run.status is KnowledgeAnswerStatus.PROCESSING:
            raise ValueError("A finished answer run cannot be processing.")
        with self._session_factory() as session:
            try:
                values = _run_values(run)
                values.pop("id")
                values.pop("tenant_id")
                values.pop("actor_id")
                values.pop("actor_roles")
                values.pop("query_hash")
                values.pop("request_id")
                values.pop("trace_id")
                values.pop("created_at")
                result = session.execute(
                    update(KnowledgeAnswerRunRecord)
                    .where(
                        KnowledgeAnswerRunRecord.id == run.id,
                        KnowledgeAnswerRunRecord.tenant_id == run.tenant_id,
                        KnowledgeAnswerRunRecord.status
                        == KnowledgeAnswerStatus.PROCESSING.value,
                    )
                    .values(**values)
                )
                if result.rowcount != 1:
                    raise KnowledgeAnswerRecordingError()
                session.add_all(
                    KnowledgeAnswerCitationRecord(
                        run_id=item.run_id,
                        citation_id=item.citation_id,
                        chunk_id=item.chunk_id,
                        source_key=item.source_key,
                        source_uri=item.source_uri,
                        document_version=item.document_version,
                        source_line_start=item.source_line_start,
                        source_line_end=item.source_line_end,
                        quote_hash=item.quote_hash,
                        claim_indexes=list(item.claim_indexes),
                    )
                    for item in citations
                )
                session.commit()
            except KnowledgeAnswerRecordingError:
                session.rollback()
                raise
            except (IntegrityError, SQLAlchemyError) as exc:
                session.rollback()
                raise KnowledgeAnswerRecordingError() from exc


def _to_record(run: KnowledgeAnswerRun) -> KnowledgeAnswerRunRecord:
    return KnowledgeAnswerRunRecord(**_run_values(run))


def _run_values(run: KnowledgeAnswerRun) -> dict[str, object]:
    return {
        "id": run.id,
        "tenant_id": run.tenant_id,
        "actor_id": run.actor_id,
        "actor_roles": [role.value for role in run.actor_roles],
        "query_hash": run.query_hash,
        "retrieval_run_id": run.retrieval_run_id,
        "status": run.status.value,
        "retrieved_candidate_count": run.retrieved_candidate_count,
        "eligible_candidate_count": run.eligible_candidate_count,
        "deduplicated_candidate_count": (
            run.deduplicated_candidate_count
        ),
        "selected_candidate_count": run.selected_candidate_count,
        "conflict_count": run.conflict_count,
        "citation_count": run.citation_count,
        "rerank_call_id": run.rerank_call_id,
        "answer_call_id": run.answer_call_id,
        "answer_hash": run.answer_hash,
        "error_code": run.error_code,
        "latency_ms": run.latency_ms,
        "request_id": run.request_id,
        "trace_id": run.trace_id,
        "created_at": run.created_at,
        "completed_at": run.completed_at,
    }
