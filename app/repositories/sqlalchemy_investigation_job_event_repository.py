from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.errors import StorageUnavailableError
from app.db.models import InvestigationJobEventRecord
from app.domain.investigation_job import (
    InvestigationJobEvent,
    InvestigationJobEventType,
    InvestigationJobStatus,
)


class SqlAlchemyInvestigationJobEventRepository:
    def __init__(self, session: Session, tenant_id: UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def add(
        self,
        event: InvestigationJobEvent,
    ) -> InvestigationJobEvent:
        if event.tenant_id != self._tenant_id:
            raise ValueError(
                "Investigation job event is outside the tenant scope."
            )
        created_at = event.created_at
        latest = self._session.scalar(
            select(func.max(InvestigationJobEventRecord.created_at)).where(
                InvestigationJobEventRecord.tenant_id == self._tenant_id,
                InvestigationJobEventRecord.job_id == event.job_id,
            )
        )
        if latest is not None:
            latest = _as_utc(latest)
            if created_at <= latest:
                created_at = latest + timedelta(microseconds=1)
        self._session.add(
            InvestigationJobEventRecord(
                id=event.id,
                tenant_id=event.tenant_id,
                job_id=event.job_id,
                event_type=event.event_type.value,
                from_status=(
                    event.from_status.value
                    if event.from_status is not None
                    else None
                ),
                to_status=event.to_status.value,
                attempt=event.attempt,
                reason=event.reason,
                request_id=event.request_id,
                trace_id=event.trace_id,
                payload=event.payload,
                created_at=created_at,
            )
        )
        return event

    def list_for_job(
        self,
        job_id: UUID,
    ) -> list[InvestigationJobEvent]:
        try:
            records = self._session.scalars(
                select(InvestigationJobEventRecord)
                .where(
                    InvestigationJobEventRecord.tenant_id
                    == self._tenant_id,
                    InvestigationJobEventRecord.job_id == job_id,
                )
                .order_by(
                    InvestigationJobEventRecord.created_at,
                    InvestigationJobEventRecord.id,
                )
            ).all()
        except SQLAlchemyError as exc:
            raise StorageUnavailableError(
                "Investigation job event query failed."
            ) from exc
        return [
            InvestigationJobEvent(
                id=record.id,
                tenant_id=record.tenant_id,
                job_id=record.job_id,
                event_type=InvestigationJobEventType(record.event_type),
                from_status=(
                    InvestigationJobStatus(record.from_status)
                    if record.from_status is not None
                    else None
                ),
                to_status=InvestigationJobStatus(record.to_status),
                attempt=record.attempt,
                reason=record.reason,
                request_id=record.request_id,
                trace_id=record.trace_id,
                payload=dict(record.payload),
                created_at=_as_utc(record.created_at),
            )
            for record in records
        ]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
