from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.errors import StorageUnavailableError
from app.db.models import TicketEventRecord
from app.domain.ticket_event import (
    TicketActorType,
    TicketEvent,
    TicketEventType,
)


class SqlAlchemyTicketEventRepository:
    def __init__(self, session: Session, tenant_id: UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def add(self, event: TicketEvent) -> TicketEvent:
        if event.tenant_id != self._tenant_id:
            raise ValueError(
                "Ticket event is outside the repository tenant scope."
            )
        self._session.add(
            TicketEventRecord(
                id=event.id,
                tenant_id=event.tenant_id,
                ticket_id=event.ticket_id,
                event_type=event.event_type.value,
                actor_type=event.actor_type.value,
                actor_id=event.actor_id,
                payload=event.payload,
                created_at=event.created_at,
            )
        )
        return event

    def list_for_ticket(self, ticket_id: UUID) -> list[TicketEvent]:
        try:
            records = self._session.scalars(
                select(TicketEventRecord)
                .where(
                    TicketEventRecord.ticket_id == ticket_id,
                    TicketEventRecord.tenant_id == self._tenant_id,
                )
                .order_by(TicketEventRecord.created_at, TicketEventRecord.id)
            ).all()
        except SQLAlchemyError as exc:
            raise StorageUnavailableError(
                "Ticket event query failed."
            ) from exc
        return [
            TicketEvent(
                id=record.id,
                tenant_id=record.tenant_id,
                ticket_id=record.ticket_id,
                event_type=TicketEventType(record.event_type),
                actor_type=TicketActorType(record.actor_type),
                actor_id=record.actor_id,
                payload=dict(record.payload),
                created_at=_as_utc(record.created_at),
            )
            for record in records
        ]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
