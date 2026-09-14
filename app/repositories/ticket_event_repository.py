from typing import Protocol
from uuid import UUID

from app.domain.ticket_event import TicketEvent


class TicketEventRepository(Protocol):
    def add(self, event: TicketEvent) -> TicketEvent: ...

    def list_for_ticket(self, ticket_id: UUID) -> list[TicketEvent]: ...
