from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import (
    ConcurrentJobUpdateError,
    DuplicateJobIdempotencyKeyError,
    StorageUnavailableError,
)
from app.db.models import InvestigationJobRecord
from app.domain.auth import Role
from app.domain.investigation_job import (
    TERMINAL_JOB_STATUSES,
    InvestigationJob,
    InvestigationJobStatus,
    InvestigationJobType,
)
from app.repositories.investigation_job_repository import (
    JobLocation,
)


class SqlAlchemyInvestigationJobRepository:
    def __init__(self, session: Session, tenant_id: UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def add(self, job: InvestigationJob) -> InvestigationJob:
        self._assert_tenant(job)
        self._session.add(self._to_record(job))
        try:
            self._session.flush()
        except IntegrityError as exc:
            self._session.rollback()
            if _is_duplicate_job_key(exc):
                raise DuplicateJobIdempotencyKeyError from exc
            raise StorageUnavailableError(
                "Investigation job write failed."
            ) from exc
        except SQLAlchemyError as exc:
            self._session.rollback()
            raise StorageUnavailableError(
                "Investigation job write failed."
            ) from exc
        return job

    def get(self, job_id: UUID) -> InvestigationJob | None:
        try:
            record = self._session.scalar(
                select(InvestigationJobRecord).where(
                    InvestigationJobRecord.id == job_id,
                    InvestigationJobRecord.tenant_id == self._tenant_id,
                )
            )
        except SQLAlchemyError as exc:
            raise StorageUnavailableError(
                "Investigation job query failed."
            ) from exc
        return None if record is None else self._to_domain(record)

    def get_by_idempotency(
        self,
        job_type: InvestigationJobType,
        idempotency_key: str,
    ) -> InvestigationJob | None:
        try:
            record = self._session.scalar(
                select(InvestigationJobRecord).where(
                    InvestigationJobRecord.tenant_id == self._tenant_id,
                    InvestigationJobRecord.job_type == job_type.value,
                    InvestigationJobRecord.idempotency_key
                    == idempotency_key,
                )
            )
        except SQLAlchemyError as exc:
            raise StorageUnavailableError(
                "Investigation job idempotency query failed."
            ) from exc
        return None if record is None else self._to_domain(record)

    def update(
        self,
        job: InvestigationJob,
        *,
        expected_version: int,
    ) -> InvestigationJob:
        self._assert_tenant(job)
        try:
            result = self._session.execute(
                update(InvestigationJobRecord)
                .where(
                    InvestigationJobRecord.id == job.id,
                    InvestigationJobRecord.tenant_id == self._tenant_id,
                    InvestigationJobRecord.version == expected_version,
                )
                .values(**self._update_values(job))
            )
        except SQLAlchemyError as exc:
            raise StorageUnavailableError(
                "Investigation job update failed."
            ) from exc
        if result.rowcount != 1:
            raise ConcurrentJobUpdateError(job.id, expected_version)
        return job

    def record_dispatch_result(
        self,
        job_id: UUID,
        *,
        dispatched_at: datetime | None,
        error_code: str | None,
    ) -> None:
        try:
            result = self._session.execute(
                update(InvestigationJobRecord)
                .where(
                    InvestigationJobRecord.id == job_id,
                    InvestigationJobRecord.tenant_id == self._tenant_id,
                )
                .values(
                    dispatched_at=dispatched_at,
                    dispatch_attempts=(
                        InvestigationJobRecord.dispatch_attempts + 1
                    ),
                    last_dispatch_error_code=error_code,
                )
            )
        except SQLAlchemyError as exc:
            raise StorageUnavailableError(
                "Investigation job dispatch state update failed."
            ) from exc
        if result.rowcount != 1:
            raise ConcurrentJobUpdateError(job_id, expected_version=-1)

    @staticmethod
    def _to_record(job: InvestigationJob) -> InvestigationJobRecord:
        return InvestigationJobRecord(
            **SqlAlchemyInvestigationJobRepository._all_values(job)
        )

    @staticmethod
    def _update_values(job: InvestigationJob) -> dict[str, object]:
        values = SqlAlchemyInvestigationJobRepository._all_values(job)
        values.pop("id")
        values.pop("tenant_id")
        values.pop("ticket_id")
        values.pop("job_type")
        values.pop("actor_id")
        values.pop("actor_roles")
        values.pop("idempotency_key")
        values.pop("request_hash")
        values.pop("request_id")
        values.pop("trace_id")
        values.pop("created_at")
        values.pop("dispatched_at")
        values.pop("dispatch_attempts")
        values.pop("last_dispatch_error_code")
        return values

    @staticmethod
    def _all_values(job: InvestigationJob) -> dict[str, object]:
        return {
            "id": job.id,
            "tenant_id": job.tenant_id,
            "ticket_id": job.ticket_id,
            "job_type": job.job_type.value,
            "status": job.status.value,
            "actor_id": job.actor_id,
            "actor_roles": sorted(role.value for role in job.actor_roles),
            "idempotency_key": job.idempotency_key,
            "request_hash": job.request_hash,
            "request_id": job.request_id,
            "trace_id": job.trace_id,
            "attempts": job.attempts,
            "max_attempts": job.max_attempts,
            "version": job.version,
            "lease_token": job.lease_token,
            "lease_expires_at": job.lease_expires_at,
            "next_attempt_at": job.next_attempt_at,
            "cancel_requested_at": job.cancel_requested_at,
            "dispatched_at": job.dispatched_at,
            "dispatch_attempts": job.dispatch_attempts,
            "last_dispatch_error_code": job.last_dispatch_error_code,
            "last_error_code": job.last_error_code,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "started_at": job.started_at,
            "completed_at": job.completed_at,
        }

    @staticmethod
    def _to_domain(record: InvestigationJobRecord) -> InvestigationJob:
        return InvestigationJob(
            id=record.id,
            tenant_id=record.tenant_id,
            ticket_id=record.ticket_id,
            job_type=InvestigationJobType(record.job_type),
            status=InvestigationJobStatus(record.status),
            actor_id=record.actor_id,
            actor_roles=frozenset(Role(role) for role in record.actor_roles),
            idempotency_key=record.idempotency_key,
            request_hash=record.request_hash,
            request_id=record.request_id,
            trace_id=record.trace_id,
            attempts=record.attempts,
            max_attempts=record.max_attempts,
            version=record.version,
            lease_token=record.lease_token,
            lease_expires_at=_optional_utc(record.lease_expires_at),
            next_attempt_at=_optional_utc(record.next_attempt_at),
            cancel_requested_at=_optional_utc(record.cancel_requested_at),
            dispatched_at=_optional_utc(record.dispatched_at),
            dispatch_attempts=record.dispatch_attempts,
            last_dispatch_error_code=record.last_dispatch_error_code,
            last_error_code=record.last_error_code,
            created_at=_as_utc(record.created_at),
            updated_at=_as_utc(record.updated_at),
            started_at=_optional_utc(record.started_at),
            completed_at=_optional_utc(record.completed_at),
        )

    def _assert_tenant(self, job: InvestigationJob) -> None:
        if job.tenant_id != self._tenant_id:
            raise ValueError(
                "Investigation job is outside the repository tenant scope."
            )


class SqlAlchemyInvestigationJobLocator:
    """System-only lookup that resolves a job id to its trusted tenant."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def locate(self, job_id: UUID) -> JobLocation | None:
        with self._session_factory() as session:
            try:
                row = session.execute(
                    select(
                        InvestigationJobRecord.id,
                        InvestigationJobRecord.tenant_id,
                    ).where(InvestigationJobRecord.id == job_id)
                ).one_or_none()
            except SQLAlchemyError as exc:
                raise StorageUnavailableError(
                    "Investigation job location lookup failed."
                ) from exc
        if row is None:
            return None
        return JobLocation(job_id=row.id, tenant_id=row.tenant_id)

    def list_pending_dispatch(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> list[JobLocation]:
        terminal_values = [status.value for status in TERMINAL_JOB_STATUSES]
        with self._session_factory() as session:
            try:
                rows = session.execute(
                    select(
                        InvestigationJobRecord.id,
                        InvestigationJobRecord.tenant_id,
                    )
                    .where(
                        InvestigationJobRecord.dispatched_at.is_(None),
                        InvestigationJobRecord.status.not_in(
                            terminal_values
                        ),
                        or_(
                            InvestigationJobRecord.next_attempt_at.is_(None),
                            InvestigationJobRecord.next_attempt_at <= now,
                        ),
                    )
                    .order_by(InvestigationJobRecord.created_at)
                    .limit(limit)
                ).all()
            except SQLAlchemyError as exc:
                raise StorageUnavailableError(
                    "Pending dispatch lookup failed."
                ) from exc
        return [
            JobLocation(job_id=row.id, tenant_id=row.tenant_id)
            for row in rows
        ]


def _is_duplicate_job_key(exc: IntegrityError) -> bool:
    diagnostic = getattr(exc.orig, "diag", None)
    if (
        getattr(diagnostic, "constraint_name", None)
        == "uq_investigation_jobs_tenant_type_key"
    ):
        return True
    message = str(exc.orig).lower()
    return (
        "investigation_jobs.tenant_id" in message
        and "investigation_jobs.job_type" in message
        and "investigation_jobs.idempotency_key" in message
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _optional_utc(value: datetime | None) -> datetime | None:
    return None if value is None else _as_utc(value)
