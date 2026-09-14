from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import StorageUnavailableError
from app.db.models import AuthorizationAuditRecord
from app.domain.auth import Permission
from app.domain.authorization_audit import (
    AuthorizationAuditEvent,
    AuthorizationDecision,
)


class SqlAlchemyAuthorizationAuditRepository:
    """Append and read authorization decisions in independent transactions."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def add(self, event: AuthorizationAuditEvent) -> None:
        with self._session_factory() as session:
            session.add(
                AuthorizationAuditRecord(
                    id=event.id,
                    tenant_id=event.tenant_id,
                    actor_id=event.actor_id,
                    permission=event.permission.value,
                    decision=event.decision.value,
                    reason=event.reason,
                    request_id=event.request_id,
                    resource_type=event.resource_type,
                    resource_id=event.resource_id,
                    created_at=event.created_at,
                )
            )
            try:
                session.commit()
            except SQLAlchemyError as exc:
                session.rollback()
                raise StorageUnavailableError(
                    "Authorization audit write failed."
                ) from exc

    def list_for_tenant(
        self,
        tenant_id: UUID,
        *,
        limit: int,
    ) -> list[AuthorizationAuditEvent]:
        with self._session_factory() as session:
            try:
                records = session.scalars(
                    select(AuthorizationAuditRecord)
                    .where(
                        AuthorizationAuditRecord.tenant_id == tenant_id
                    )
                    .order_by(
                        AuthorizationAuditRecord.created_at.desc(),
                        AuthorizationAuditRecord.id.desc(),
                    )
                    .limit(limit)
                ).all()
            except SQLAlchemyError as exc:
                raise StorageUnavailableError(
                    "Authorization audit query failed."
                ) from exc
        return [
            AuthorizationAuditEvent(
                id=record.id,
                tenant_id=record.tenant_id,
                actor_id=record.actor_id,
                permission=Permission(record.permission),
                decision=AuthorizationDecision(record.decision),
                reason=record.reason,
                request_id=record.request_id,
                resource_type=record.resource_type,
                resource_id=record.resource_id,
                created_at=_as_utc(record.created_at),
            )
            for record in records
        ]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
