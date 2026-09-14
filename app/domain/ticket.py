from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class TicketCategory(StrEnum):
    NOT_RECEIVED = "not_received"
    DAMAGED = "damaged"
    WRONG_ITEM = "wrong_item"
    REFUND_FAILED = "refund_failed"
    OTHER = "other"


class TicketStatus(StrEnum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    WAITING_FOR_CUSTOMER = "waiting_for_customer"
    PENDING_APPROVAL = "pending_approval"
    RESOLVED = "resolved"
    CLOSED = "closed"


ALLOWED_TICKET_TRANSITIONS: dict[TicketStatus, frozenset[TicketStatus]] = {
    TicketStatus.OPEN: frozenset({TicketStatus.INVESTIGATING}),
    TicketStatus.INVESTIGATING: frozenset(
        {
            TicketStatus.WAITING_FOR_CUSTOMER,
            TicketStatus.PENDING_APPROVAL,
            TicketStatus.RESOLVED,
        }
    ),
    TicketStatus.WAITING_FOR_CUSTOMER: frozenset(
        {TicketStatus.INVESTIGATING}
    ),
    TicketStatus.PENDING_APPROVAL: frozenset(
        {TicketStatus.INVESTIGATING, TicketStatus.RESOLVED}
    ),
    TicketStatus.RESOLVED: frozenset(
        {TicketStatus.INVESTIGATING, TicketStatus.CLOSED}
    ),
    TicketStatus.CLOSED: frozenset(),
}


class InvalidTicketTransitionError(ValueError):
    def __init__(
        self,
        current_status: TicketStatus,
        target_status: TicketStatus,
    ) -> None:
        self.current_status = current_status
        self.target_status = target_status
        self.allowed_statuses = ALLOWED_TICKET_TRANSITIONS[current_status]
        super().__init__(
            f"Ticket cannot transition from {current_status} to {target_status}."
        )


@dataclass(frozen=True, slots=True)
class Ticket:
    id: UUID
    tenant_id: UUID
    customer_id: str
    subject: str
    description: str
    category: TicketCategory
    status: TicketStatus
    created_at: datetime
    updated_at: datetime
    version: int = 1

    def transition_to(
        self,
        target_status: TicketStatus,
        *,
        changed_at: datetime,
    ) -> "Ticket":
        if target_status not in ALLOWED_TICKET_TRANSITIONS[self.status]:
            raise InvalidTicketTransitionError(self.status, target_status)
        return replace(
            self,
            status=target_status,
            updated_at=changed_at,
            version=self.version + 1,
        )
