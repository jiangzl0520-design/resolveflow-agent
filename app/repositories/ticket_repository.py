from threading import RLock
from typing import Protocol
from uuid import UUID

from app.core.errors import ConcurrentTicketUpdateError
from app.domain.ticket import Ticket


class TicketRepository(Protocol):
    """Persistence contract required by the ticket application service."""

    def add(self, ticket: Ticket) -> Ticket: ...

    def get(self, ticket_id: UUID) -> Ticket | None: ...

    def update(self, ticket: Ticket, *, expected_version: int) -> Ticket: ...


class InMemoryTicketRepository:
    """Day 1 adapter; Day 2 will replace it with PostgreSQL."""

    def __init__(self, tenant_id: UUID) -> None:
        self._tenant_id = tenant_id
        self._tickets: dict[UUID, Ticket] = {}
        self._lock = RLock()

    def add(self, ticket: Ticket) -> Ticket:
        self._assert_tenant(ticket)
        with self._lock:
            self._tickets[ticket.id] = ticket
        return ticket

    def get(self, ticket_id: UUID) -> Ticket | None:
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None or ticket.tenant_id != self._tenant_id:
                return None
            return ticket

    def update(self, ticket: Ticket, *, expected_version: int) -> Ticket:
        self._assert_tenant(ticket)
        with self._lock:
            current = self._tickets.get(ticket.id)
            if current is None or current.version != expected_version:
                raise ConcurrentTicketUpdateError(ticket.id, expected_version)
            self._tickets[ticket.id] = ticket
        return ticket

    def _assert_tenant(self, ticket: Ticket) -> None:
        if ticket.tenant_id != self._tenant_id:
            raise ValueError("Ticket is outside the repository tenant scope.")
