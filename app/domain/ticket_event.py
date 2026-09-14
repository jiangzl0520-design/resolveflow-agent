from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class TicketEventType(StrEnum):
    CREATED = "ticket_created"
    STATUS_CHANGED = "ticket_status_changed"


class TicketActorType(StrEnum):
    CUSTOMER = "customer"
    STAFF = "staff"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class TicketEvent:
    id: UUID
    tenant_id: UUID
    ticket_id: UUID
    event_type: TicketEventType
    actor_type: TicketActorType
    actor_id: str
    payload: dict[str, Any]
    created_at: datetime
