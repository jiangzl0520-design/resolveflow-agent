from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    tenant_id: UUID
    operation: str
    key: str
    request_hash: str
    resource_id: UUID
    created_at: datetime
