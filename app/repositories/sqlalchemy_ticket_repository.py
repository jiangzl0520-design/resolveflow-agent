from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.errors import (
    ConcurrentTicketUpdateError,
    StorageUnavailableError,
)
from app.db.models import TicketRecord
from app.domain.ticket import Ticket, TicketCategory, TicketStatus


class SqlAlchemyTicketRepository:
    """Persist Ticket domain entities through short-lived SQLAlchemy sessions."""

    def __init__(self, session: Session, tenant_id: UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def add(self, ticket: Ticket) -> Ticket:
        self._assert_tenant(ticket)
        self._session.add(self._to_record(ticket))
        return ticket

    def get(self, ticket_id: UUID) -> Ticket | None:
        try:
            record = self._session.scalar(
                select(TicketRecord).where(
                    TicketRecord.id == ticket_id,
                    TicketRecord.tenant_id == self._tenant_id,
                )
            )
        except SQLAlchemyError as exc:
            raise StorageUnavailableError(
                "Ticket storage query failed."
            ) from exc

        if record is None:
            return None
        return self._to_domain(record)

    def update(self, ticket: Ticket, *, expected_version: int) -> Ticket:
        self._assert_tenant(ticket)
        try:
            result = self._session.execute(
                update(TicketRecord)
                .where(
                    TicketRecord.id == ticket.id,
                    TicketRecord.tenant_id == self._tenant_id,
                    TicketRecord.version == expected_version,
                )
                .values(
                    status=ticket.status.value,
                    updated_at=ticket.updated_at,
                    version=ticket.version,
                )
            )
        except SQLAlchemyError as exc:
            raise StorageUnavailableError(
                "Ticket storage update failed."
            ) from exc
        if result.rowcount != 1:
            raise ConcurrentTicketUpdateError(ticket.id, expected_version)
        return ticket

    @staticmethod
    def _to_record(ticket: Ticket) -> TicketRecord:
        return TicketRecord(
            id=ticket.id,
            tenant_id=ticket.tenant_id,
            customer_id=ticket.customer_id,
            subject=ticket.subject,
            description=ticket.description,
            category=ticket.category.value,
            status=ticket.status.value,
            created_at=ticket.created_at,
            updated_at=ticket.updated_at,
            version=ticket.version,
        )

    @staticmethod
    def _to_domain(record: TicketRecord) -> Ticket:
        return Ticket(
            id=record.id,
            tenant_id=record.tenant_id,
            customer_id=record.customer_id,
            subject=record.subject,
            description=record.description,
            category=TicketCategory(record.category),
            status=TicketStatus(record.status),
            created_at=_as_utc(record.created_at),
            updated_at=_as_utc(record.updated_at),
            version=record.version,
        )

    def _assert_tenant(self, ticket: Ticket) -> None:
        if ticket.tenant_id != self._tenant_id:
            raise ValueError("Ticket is outside the repository tenant scope.")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
