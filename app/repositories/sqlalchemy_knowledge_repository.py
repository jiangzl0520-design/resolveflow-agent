from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import StorageUnavailableError
from app.domain.auth import Role
from app.db.models import (
    KnowledgeChunkRecord,
    KnowledgeDocumentRecord,
    KnowledgeIngestionRunRecord,
)
from app.domain.knowledge import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIngestionRun,
    KnowledgeIngestionStatus,
)


class SqlAlchemyKnowledgeRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get_run_by_idempotency_key(
        self,
        tenant_id: UUID,
        idempotency_key: str,
    ) -> KnowledgeIngestionRun | None:
        with self._session_factory() as session:
            try:
                record = session.scalar(
                    select(KnowledgeIngestionRunRecord).where(
                        KnowledgeIngestionRunRecord.tenant_id == tenant_id,
                        KnowledgeIngestionRunRecord.idempotency_key
                        == idempotency_key,
                    )
                )
            except SQLAlchemyError as exc:
                raise StorageUnavailableError(
                    "Knowledge ingestion run query failed."
                ) from exc
        return _run_to_domain(record) if record is not None else None

    def add_run(self, run: KnowledgeIngestionRun) -> None:
        with self._session_factory() as session:
            session.add(_run_to_record(run))
            self._commit(session, "Knowledge ingestion run write failed.")

    def save_run(self, run: KnowledgeIngestionRun) -> None:
        with self._session_factory() as session:
            try:
                record = session.get(KnowledgeIngestionRunRecord, run.id)
                if record is None or record.tenant_id != run.tenant_id:
                    raise StorageUnavailableError(
                        "Knowledge ingestion run does not exist."
                    )
                _apply_run(record, run)
                session.commit()
            except StorageUnavailableError:
                session.rollback()
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                raise StorageUnavailableError(
                    "Knowledge ingestion run update failed."
                ) from exc

    def find_document_by_source_version(
        self,
        tenant_id: UUID,
        source_key: str,
        document_version: str,
    ) -> KnowledgeDocument | None:
        with self._session_factory() as session:
            try:
                record = session.scalar(
                    select(KnowledgeDocumentRecord).where(
                        KnowledgeDocumentRecord.tenant_id == tenant_id,
                        KnowledgeDocumentRecord.source_key == source_key,
                        KnowledgeDocumentRecord.document_version
                        == document_version,
                    )
                )
            except SQLAlchemyError as exc:
                raise StorageUnavailableError(
                    "Knowledge document query failed."
                ) from exc
        return _document_to_domain(record) if record is not None else None

    def get_document(
        self,
        tenant_id: UUID,
        document_id: UUID,
    ) -> KnowledgeDocument | None:
        with self._session_factory() as session:
            try:
                record = session.scalar(
                    select(KnowledgeDocumentRecord).where(
                        KnowledgeDocumentRecord.id == document_id,
                        KnowledgeDocumentRecord.tenant_id == tenant_id,
                    )
                )
            except SQLAlchemyError as exc:
                raise StorageUnavailableError(
                    "Knowledge document read failed."
                ) from exc
        return _document_to_domain(record) if record is not None else None

    def list_chunks(
        self,
        tenant_id: UUID,
        document_id: UUID,
    ) -> list[KnowledgeChunk]:
        with self._session_factory() as session:
            try:
                records = session.scalars(
                    select(KnowledgeChunkRecord)
                    .where(
                        KnowledgeChunkRecord.tenant_id == tenant_id,
                        KnowledgeChunkRecord.document_id == document_id,
                    )
                    .order_by(KnowledgeChunkRecord.chunk_index)
                ).all()
            except SQLAlchemyError as exc:
                raise StorageUnavailableError(
                    "Knowledge chunk query failed."
                ) from exc
        return [_chunk_to_domain(record) for record in records]

    def publish(
        self,
        run: KnowledgeIngestionRun,
        document: KnowledgeDocument,
        chunks: tuple[KnowledgeChunk, ...],
    ) -> None:
        with self._session_factory() as session:
            try:
                session.execute(
                    update(KnowledgeDocumentRecord)
                    .where(
                        KnowledgeDocumentRecord.tenant_id
                        == document.tenant_id,
                        KnowledgeDocumentRecord.source_key
                        == document.source_key,
                        KnowledgeDocumentRecord.is_current.is_(True),
                    )
                    .values(is_current=False)
                )
                session.add(_document_to_record(document))
                # No ORM relationship connects these persistence records, so
                # make the parent row visible inside this transaction before
                # PostgreSQL validates the chunks' foreign keys.
                session.flush()
                session.add_all(_chunk_to_record(chunk) for chunk in chunks)
                run_record = session.get(
                    KnowledgeIngestionRunRecord,
                    run.id,
                )
                if run_record is None or run_record.tenant_id != run.tenant_id:
                    raise StorageUnavailableError(
                        "Knowledge ingestion run does not exist."
                    )
                _apply_run(run_record, run)
                session.commit()
            except StorageUnavailableError:
                session.rollback()
                raise
            except (IntegrityError, SQLAlchemyError) as exc:
                session.rollback()
                raise StorageUnavailableError(
                    "Knowledge document publish failed."
                ) from exc

    @staticmethod
    def _commit(session: Session, message: str) -> None:
        try:
            session.commit()
        except (IntegrityError, SQLAlchemyError) as exc:
            session.rollback()
            raise StorageUnavailableError(message) from exc


def _run_to_record(run: KnowledgeIngestionRun) -> KnowledgeIngestionRunRecord:
    return KnowledgeIngestionRunRecord(
        id=run.id,
        tenant_id=run.tenant_id,
        idempotency_key=run.idempotency_key,
        request_hash=run.request_hash,
        source_key=run.source_key,
        document_version=run.document_version,
        content_hash=run.content_hash,
        status=run.status.value,
        attempts=run.attempts,
        max_attempts=run.max_attempts,
        document_id=run.document_id,
        chunk_count=run.chunk_count,
        error_code=run.error_code,
        request_id=run.request_id,
        trace_id=run.trace_id,
        created_at=run.created_at,
        updated_at=run.updated_at,
        completed_at=run.completed_at,
    )


def _apply_run(
    record: KnowledgeIngestionRunRecord,
    run: KnowledgeIngestionRun,
) -> None:
    record.status = run.status.value
    record.attempts = run.attempts
    record.max_attempts = run.max_attempts
    record.document_id = run.document_id
    record.chunk_count = run.chunk_count
    record.error_code = run.error_code
    record.request_id = run.request_id
    record.trace_id = run.trace_id
    record.updated_at = run.updated_at
    record.completed_at = run.completed_at


def _document_to_record(
    document: KnowledgeDocument,
) -> KnowledgeDocumentRecord:
    return KnowledgeDocumentRecord(
        id=document.id,
        tenant_id=document.tenant_id,
        source_key=document.source_key,
        document_version=document.document_version,
        title=document.title,
        source_uri=document.source_uri,
        media_type=document.media_type,
        content_hash=document.content_hash,
        raw_content=document.raw_content,
        effective_from=document.effective_from,
        effective_to=document.effective_to,
        allowed_roles=[role.value for role in document.allowed_roles],
        is_current=document.is_current,
        created_at=document.created_at,
    )


def _chunk_to_record(chunk: KnowledgeChunk) -> KnowledgeChunkRecord:
    return KnowledgeChunkRecord(
        id=chunk.id,
        tenant_id=chunk.tenant_id,
        document_id=chunk.document_id,
        chunk_index=chunk.chunk_index,
        section_path=list(chunk.section_path),
        content=chunk.content,
        content_hash=chunk.content_hash,
        source_uri=chunk.source_uri,
        document_version=chunk.document_version,
        source_line_start=chunk.source_line_start,
        source_line_end=chunk.source_line_end,
        effective_from=chunk.effective_from,
        effective_to=chunk.effective_to,
        allowed_roles=[role.value for role in chunk.allowed_roles],
        created_at=chunk.created_at,
    )


def _run_to_domain(record: KnowledgeIngestionRunRecord) -> KnowledgeIngestionRun:
    return KnowledgeIngestionRun(
        id=record.id,
        tenant_id=record.tenant_id,
        idempotency_key=record.idempotency_key,
        request_hash=record.request_hash,
        source_key=record.source_key,
        document_version=record.document_version,
        content_hash=record.content_hash,
        status=KnowledgeIngestionStatus(record.status),
        attempts=record.attempts,
        max_attempts=record.max_attempts,
        document_id=record.document_id,
        chunk_count=record.chunk_count,
        error_code=record.error_code,
        request_id=record.request_id,
        trace_id=record.trace_id,
        created_at=_as_utc(record.created_at),
        updated_at=_as_utc(record.updated_at),
        completed_at=(
            _as_utc(record.completed_at)
            if record.completed_at is not None
            else None
        ),
    )


def _document_to_domain(record: KnowledgeDocumentRecord) -> KnowledgeDocument:
    return KnowledgeDocument(
        id=record.id,
        tenant_id=record.tenant_id,
        source_key=record.source_key,
        document_version=record.document_version,
        title=record.title,
        source_uri=record.source_uri,
        media_type=record.media_type,
        content_hash=record.content_hash,
        raw_content=record.raw_content,
        effective_from=_as_utc(record.effective_from),
        effective_to=(
            _as_utc(record.effective_to)
            if record.effective_to is not None
            else None
        ),
        allowed_roles=tuple(Role(role) for role in record.allowed_roles),
        is_current=record.is_current,
        created_at=_as_utc(record.created_at),
    )


def _chunk_to_domain(record: KnowledgeChunkRecord) -> KnowledgeChunk:
    return KnowledgeChunk(
        id=record.id,
        tenant_id=record.tenant_id,
        document_id=record.document_id,
        chunk_index=record.chunk_index,
        section_path=tuple(record.section_path),
        content=record.content,
        content_hash=record.content_hash,
        source_uri=record.source_uri,
        document_version=record.document_version,
        source_line_start=record.source_line_start,
        source_line_end=record.source_line_end,
        effective_from=_as_utc(record.effective_from),
        effective_to=(
            _as_utc(record.effective_to)
            if record.effective_to is not None
            else None
        ),
        allowed_roles=tuple(Role(role) for role in record.allowed_roles),
        created_at=_as_utc(record.created_at),
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
