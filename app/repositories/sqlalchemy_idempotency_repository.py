from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.errors import (
    DuplicateIdempotencyKeyError,
    StorageUnavailableError,
)
from app.db.models import IdempotencyRecordModel
from app.domain.idempotency import IdempotencyRecord


class SqlAlchemyIdempotencyRepository:
    def __init__(self, session: Session, tenant_id: UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def get(self, operation: str, key: str) -> IdempotencyRecord | None:
        try:
            model = self._session.get(
                IdempotencyRecordModel,
                {
                    "tenant_id": self._tenant_id,
                    "operation": operation,
                    "key": key,
                },
            )
        except SQLAlchemyError as exc:
            raise StorageUnavailableError(
                "Idempotency storage query failed."
            ) from exc
        if model is None:
            return None
        return IdempotencyRecord(
            tenant_id=model.tenant_id,
            operation=model.operation,
            key=model.key,
            request_hash=model.request_hash,
            resource_id=model.resource_id,
            created_at=_as_utc(model.created_at),
        )

    def add(self, record: IdempotencyRecord) -> IdempotencyRecord:
        if record.tenant_id != self._tenant_id:
            raise ValueError(
                "Idempotency record is outside the repository tenant scope."
            )
        self._session.add(
            IdempotencyRecordModel(
                tenant_id=record.tenant_id,
                operation=record.operation,
                key=record.key,
                request_hash=record.request_hash,
                resource_id=record.resource_id,
                created_at=record.created_at,
            )
        )
        try:
            self._session.flush()
        except IntegrityError as exc:
            self._session.rollback()
            if _is_duplicate_idempotency_key(exc):
                raise DuplicateIdempotencyKeyError from exc
            raise StorageUnavailableError(
                "Idempotency storage write failed."
            ) from exc
        except SQLAlchemyError as exc:
            self._session.rollback()
            raise StorageUnavailableError(
                "Idempotency storage write failed."
            ) from exc
        return record


def _is_duplicate_idempotency_key(exc: IntegrityError) -> bool:
    diagnostic = getattr(exc.orig, "diag", None)
    constraint_name = getattr(diagnostic, "constraint_name", None)
    if constraint_name == "pk_idempotency_keys":
        return True
    message = str(exc.orig).lower()
    return (
        "idempotency_keys.tenant_id" in message
        and "idempotency_keys.operation" in message
        and "idempotency_keys.key" in message
    ) or (
        "idempotency_keys.operation" in message
        and "idempotency_keys.key" in message
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
