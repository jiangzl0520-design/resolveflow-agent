from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    LongTermMemoryRecord,
    MemoryAuditEventRecord,
    TicketRecord,
)
from app.domain.memory import (
    LongTermMemory,
    MemoryAuditEvent,
    MemoryCategory,
    MemoryDecision,
    MemoryMutationResolver,
    MemoryOperationResult,
    MemorySourceType,
    MemoryStatus,
    MemoryTransaction,
)
from app.memory.errors import (
    MemoryIdempotencyConflictError,
    MemoryReadError,
    MemoryStorageError,
)


class SqlAlchemyMemoryRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def transact(
        self,
        transaction: MemoryTransaction,
        resolver: MemoryMutationResolver,
    ) -> MemoryOperationResult:
        with self._session_factory() as session:
            try:
                previous = self._event_for_key(
                    session,
                    transaction.tenant_id,
                    transaction.idempotency_key,
                )
                if previous is not None:
                    return _replay(previous, transaction.request_hash)
                models = session.scalars(
                    select(LongTermMemoryRecord)
                    .where(
                        LongTermMemoryRecord.tenant_id
                        == transaction.tenant_id,
                        LongTermMemoryRecord.subject_id
                        == transaction.subject_id,
                        LongTermMemoryRecord.memory_key
                        == transaction.memory_key,
                    )
                    .order_by(LongTermMemoryRecord.version)
                    .with_for_update()
                ).all()
                mutation = resolver(tuple(_to_domain(item) for item in models))
                by_id = {item.id: item for item in models}
                for memory in mutation.updated:
                    model = by_id.get(memory.id)
                    if model is None:
                        raise MemoryStorageError()
                    _apply(model, memory)
                if mutation.created is not None:
                    session.add(_to_record(mutation.created))
                event = MemoryAuditEvent(
                    id=uuid4(),
                    tenant_id=transaction.tenant_id,
                    subject_id=transaction.subject_id,
                    memory_key=transaction.memory_key,
                    memory_id=mutation.result.memory_id,
                    actor_id=transaction.actor_id,
                    decision=mutation.result.decision,
                    request_hash=transaction.request_hash,
                    candidate_hash=transaction.candidate_hash,
                    idempotency_key=transaction.idempotency_key,
                    request_id=transaction.request_id,
                    trace_id=transaction.trace_id,
                    created_at=transaction.created_at,
                )
                session.add(_event_to_record(event))
                session.commit()
                return mutation.result
            except MemoryIdempotencyConflictError:
                session.rollback()
                raise
            except IntegrityError as exc:
                session.rollback()
                previous = self._event_for_key(
                    session,
                    transaction.tenant_id,
                    transaction.idempotency_key,
                )
                if previous is not None:
                    return _replay(previous, transaction.request_hash)
                raise MemoryStorageError() from exc
            except (SQLAlchemyError, MemoryStorageError) as exc:
                session.rollback()
                if isinstance(exc, MemoryStorageError):
                    raise
                raise MemoryStorageError() from exc

    def list_active_for_subject(
        self,
        tenant_id: UUID,
        subject_id: str,
        *,
        at: datetime,
    ) -> tuple[LongTermMemory, ...]:
        with self._session_factory() as session:
            try:
                records = session.scalars(
                    select(LongTermMemoryRecord)
                    .where(
                        LongTermMemoryRecord.tenant_id == tenant_id,
                        LongTermMemoryRecord.subject_id == subject_id,
                        LongTermMemoryRecord.status
                        == MemoryStatus.ACTIVE.value,
                        LongTermMemoryRecord.expires_at > at,
                    )
                    .order_by(
                        LongTermMemoryRecord.memory_key,
                        LongTermMemoryRecord.version.desc(),
                    )
                ).all()
            except SQLAlchemyError as exc:
                raise MemoryReadError() from exc
        return tuple(_to_domain(item) for item in records)

    def list_active_for_ticket(
        self,
        tenant_id: UUID,
        ticket_id: UUID,
        *,
        at: datetime,
    ) -> tuple[LongTermMemory, ...]:
        with self._session_factory() as session:
            try:
                records = session.scalars(
                    select(LongTermMemoryRecord)
                    .join(
                        TicketRecord,
                        and_(
                            TicketRecord.tenant_id
                            == LongTermMemoryRecord.tenant_id,
                            TicketRecord.customer_id
                            == LongTermMemoryRecord.subject_id,
                        ),
                    )
                    .where(
                        TicketRecord.id == ticket_id,
                        TicketRecord.tenant_id == tenant_id,
                        LongTermMemoryRecord.tenant_id == tenant_id,
                        LongTermMemoryRecord.status
                        == MemoryStatus.ACTIVE.value,
                        LongTermMemoryRecord.expires_at > at,
                    )
                    .order_by(
                        LongTermMemoryRecord.memory_key,
                        LongTermMemoryRecord.version.desc(),
                    )
                ).all()
            except SQLAlchemyError as exc:
                raise MemoryReadError() from exc
        return tuple(_to_domain(item) for item in records)

    @staticmethod
    def _event_for_key(
        session: Session,
        tenant_id: UUID,
        idempotency_key: str,
    ) -> MemoryAuditEventRecord | None:
        return session.scalar(
            select(MemoryAuditEventRecord).where(
                MemoryAuditEventRecord.tenant_id == tenant_id,
                MemoryAuditEventRecord.idempotency_key == idempotency_key,
            )
        )


def _replay(
    event: MemoryAuditEventRecord,
    request_hash: str,
) -> MemoryOperationResult:
    if event.request_hash != request_hash:
        raise MemoryIdempotencyConflictError()
    return MemoryOperationResult(
        decision=MemoryDecision(event.decision),
        memory_id=event.memory_id,
        memory_key=event.memory_key,
    )


def _to_record(memory: LongTermMemory) -> LongTermMemoryRecord:
    return LongTermMemoryRecord(
        id=memory.id,
        tenant_id=memory.tenant_id,
        subject_id=memory.subject_id,
        memory_key=memory.memory_key,
        category=memory.category.value,
        value=memory.value,
        value_hash=memory.value_hash,
        status=memory.status.value,
        version=memory.version,
        source_type=memory.source_type.value,
        source_reference_hash=memory.source_reference_hash,
        confidence=memory.confidence,
        observed_at=memory.observed_at,
        expires_at=memory.expires_at,
        created_by_actor_id=memory.created_by_actor_id,
        created_at=memory.created_at,
        updated_at=memory.updated_at,
    )


def _apply(record: LongTermMemoryRecord, memory: LongTermMemory) -> None:
    record.value = memory.value
    record.status = memory.status.value
    record.updated_at = memory.updated_at


def _event_to_record(event: MemoryAuditEvent) -> MemoryAuditEventRecord:
    return MemoryAuditEventRecord(
        id=event.id,
        tenant_id=event.tenant_id,
        subject_id=event.subject_id,
        memory_key=event.memory_key,
        memory_id=event.memory_id,
        actor_id=event.actor_id,
        decision=event.decision.value,
        request_hash=event.request_hash,
        candidate_hash=event.candidate_hash,
        idempotency_key=event.idempotency_key,
        request_id=event.request_id,
        trace_id=event.trace_id,
        created_at=event.created_at,
    )


def _to_domain(record: LongTermMemoryRecord) -> LongTermMemory:
    return LongTermMemory(
        id=record.id,
        tenant_id=record.tenant_id,
        subject_id=record.subject_id,
        memory_key=record.memory_key,
        category=MemoryCategory(record.category),
        value=record.value,
        value_hash=record.value_hash,
        status=MemoryStatus(record.status),
        version=record.version,
        source_type=MemorySourceType(record.source_type),
        source_reference_hash=record.source_reference_hash,
        confidence=record.confidence,
        observed_at=_as_utc(record.observed_at),
        expires_at=_as_utc(record.expires_at),
        created_by_actor_id=record.created_by_actor_id,
        created_at=_as_utc(record.created_at),
        updated_at=_as_utc(record.updated_at),
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
