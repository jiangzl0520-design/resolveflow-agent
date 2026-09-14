from typing import Protocol

from app.domain.idempotency import IdempotencyRecord


class IdempotencyRepository(Protocol):
    def get(self, operation: str, key: str) -> IdempotencyRecord | None: ...

    def add(self, record: IdempotencyRecord) -> IdempotencyRecord: ...
