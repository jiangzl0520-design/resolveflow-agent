from threading import RLock
from typing import Protocol
from uuid import UUID

from app.domain.model_call import ModelCallRecord


class ModelCallRepository(Protocol):
    def add(self, record: ModelCallRecord) -> None: ...

    def list_for_trace(
        self,
        tenant_id: UUID,
        trace_id: str,
    ) -> list[ModelCallRecord]: ...


class InMemoryModelCallRepository:
    def __init__(self) -> None:
        self._records: list[ModelCallRecord] = []
        self._lock = RLock()

    def add(self, record: ModelCallRecord) -> None:
        with self._lock:
            self._records.append(record)

    def list_for_trace(
        self,
        tenant_id: UUID,
        trace_id: str,
    ) -> list[ModelCallRecord]:
        with self._lock:
            return [
                record
                for record in self._records
                if record.tenant_id == tenant_id
                and record.trace_id == trace_id
            ]

    @property
    def records(self) -> list[ModelCallRecord]:
        with self._lock:
            return list(self._records)
