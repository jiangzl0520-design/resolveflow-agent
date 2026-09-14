from threading import RLock
from typing import Protocol
from uuid import UUID, uuid4

from app.domain.memory import (
    LongTermMemory,
    MemoryAuditEvent,
    MemoryMutationResolver,
    MemoryOperationResult,
    MemoryStatus,
    MemoryTransaction,
)
from app.memory.errors import MemoryIdempotencyConflictError


class MemoryRepository(Protocol):
    def transact(
        self,
        transaction: MemoryTransaction,
        resolver: MemoryMutationResolver,
    ) -> MemoryOperationResult: ...

    def list_active_for_subject(
        self,
        tenant_id: UUID,
        subject_id: str,
        *,
        at,
    ) -> tuple[LongTermMemory, ...]: ...

    def list_active_for_ticket(
        self,
        tenant_id: UUID,
        ticket_id: UUID,
        *,
        at,
    ) -> tuple[LongTermMemory, ...]: ...


class InMemoryMemoryRepository:
    def __init__(self) -> None:
        self._memories: dict[UUID, LongTermMemory] = {}
        self._events: list[MemoryAuditEvent] = []
        self._event_keys: dict[tuple[UUID, str], MemoryAuditEvent] = {}
        self._ticket_subjects: dict[tuple[UUID, UUID], str] = {}
        self._lock = RLock()

    def bind_ticket(
        self,
        tenant_id: UUID,
        ticket_id: UUID,
        subject_id: str,
    ) -> None:
        with self._lock:
            self._ticket_subjects[(tenant_id, ticket_id)] = subject_id

    def transact(
        self,
        transaction: MemoryTransaction,
        resolver: MemoryMutationResolver,
    ) -> MemoryOperationResult:
        with self._lock:
            key = (transaction.tenant_id, transaction.idempotency_key)
            previous = self._event_keys.get(key)
            if previous is not None:
                if previous.request_hash != transaction.request_hash:
                    raise MemoryIdempotencyConflictError()
                return MemoryOperationResult(
                    decision=previous.decision,
                    memory_id=previous.memory_id,
                    memory_key=previous.memory_key,
                )
            records = tuple(
                sorted(
                    (
                        memory
                        for memory in self._memories.values()
                        if memory.tenant_id == transaction.tenant_id
                        and memory.subject_id == transaction.subject_id
                        and memory.memory_key == transaction.memory_key
                    ),
                    key=lambda item: item.version,
                )
            )
            mutation = resolver(records)
            for memory in mutation.updated:
                self._memories[memory.id] = memory
            if mutation.created is not None:
                self._memories[mutation.created.id] = mutation.created
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
            self._events.append(event)
            self._event_keys[key] = event
            return mutation.result

    def list_active_for_subject(
        self,
        tenant_id: UUID,
        subject_id: str,
        *,
        at,
    ) -> tuple[LongTermMemory, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        memory
                        for memory in self._memories.values()
                        if memory.tenant_id == tenant_id
                        and memory.subject_id == subject_id
                        and memory.status is MemoryStatus.ACTIVE
                        and memory.expires_at > at
                    ),
                    key=lambda item: (item.memory_key, -item.version),
                )
            )

    def list_active_for_ticket(
        self,
        tenant_id: UUID,
        ticket_id: UUID,
        *,
        at,
    ) -> tuple[LongTermMemory, ...]:
        with self._lock:
            subject_id = self._ticket_subjects.get((tenant_id, ticket_id))
        if subject_id is None:
            return ()
        return self.list_active_for_subject(tenant_id, subject_id, at=at)

    @property
    def memories(self) -> list[LongTermMemory]:
        with self._lock:
            return list(self._memories.values())

    @property
    def events(self) -> list[MemoryAuditEvent]:
        with self._lock:
            return list(self._events)
