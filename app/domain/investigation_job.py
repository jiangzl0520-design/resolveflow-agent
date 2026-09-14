from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from math import ceil
from typing import Any
from uuid import UUID

from app.domain.auth import Role


class InvestigationJobType(StrEnum):
    START_INVESTIGATION = "start_investigation"


class InvestigationJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRY_SCHEDULED = "retry_scheduled"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InvestigationJobEventType(StrEnum):
    CREATED = "job_created"
    DISPATCHED = "job_dispatched"
    DISPATCH_FAILED = "job_dispatch_failed"
    STARTED = "job_started"
    RETRY_SCHEDULED = "job_retry_scheduled"
    CANCEL_REQUESTED = "job_cancel_requested"
    CANCELLED = "job_cancelled"
    SUCCEEDED = "job_succeeded"
    FAILED = "job_failed"
    LEASE_RECOVERED = "job_lease_recovered"


TERMINAL_JOB_STATUSES = frozenset(
    {
        InvestigationJobStatus.SUCCEEDED,
        InvestigationJobStatus.FAILED,
        InvestigationJobStatus.CANCELLED,
    }
)


class JobNotClaimableError(Exception):
    def __init__(self, reason: str, retry_after_seconds: int | None = None):
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds
        super().__init__(reason)


class JobStateTransitionError(Exception):
    def __init__(
        self,
        current_status: InvestigationJobStatus,
        operation: str,
    ) -> None:
        self.current_status = current_status
        self.operation = operation
        super().__init__(
            f"Cannot {operation} job while status is {current_status}."
        )


@dataclass(frozen=True, slots=True)
class InvestigationJob:
    id: UUID
    tenant_id: UUID
    ticket_id: UUID
    job_type: InvestigationJobType
    status: InvestigationJobStatus
    actor_id: str
    actor_roles: frozenset[Role]
    idempotency_key: str
    request_hash: str
    request_id: str
    trace_id: str
    attempts: int
    max_attempts: int
    version: int
    lease_token: UUID | None
    lease_expires_at: datetime | None
    next_attempt_at: datetime | None
    cancel_requested_at: datetime | None
    dispatched_at: datetime | None
    dispatch_attempts: int
    last_dispatch_error_code: str | None
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_JOB_STATUSES

    def claim(
        self,
        *,
        now: datetime,
        lease_token: UUID,
        lease_duration: timedelta,
    ) -> tuple["InvestigationJob", bool]:
        if self.is_terminal:
            raise JobNotClaimableError("terminal")
        if self.status is InvestigationJobStatus.CANCEL_REQUESTED:
            raise JobNotClaimableError("cancel_requested")
        if self.attempts >= self.max_attempts:
            raise JobNotClaimableError("attempts_exhausted")
        if (
            self.status is InvestigationJobStatus.RETRY_SCHEDULED
            and self.next_attempt_at is not None
            and self.next_attempt_at > now
        ):
            retry_after = max(
                1,
                ceil((self.next_attempt_at - now).total_seconds()),
            )
            raise JobNotClaimableError(
                "retry_not_due",
                retry_after_seconds=retry_after,
            )
        recovered = False
        if self.status is InvestigationJobStatus.RUNNING:
            if (
                self.lease_expires_at is not None
                and self.lease_expires_at > now
            ):
                retry_after = max(
                    1,
                    ceil((self.lease_expires_at - now).total_seconds()),
                )
                raise JobNotClaimableError(
                    "lease_active",
                    retry_after_seconds=retry_after,
                )
            recovered = True
        elif self.status not in {
            InvestigationJobStatus.QUEUED,
            InvestigationJobStatus.RETRY_SCHEDULED,
        }:
            raise JobNotClaimableError("status_not_claimable")

        return (
            replace(
                self,
                status=InvestigationJobStatus.RUNNING,
                attempts=self.attempts + 1,
                version=self.version + 1,
                lease_token=lease_token,
                lease_expires_at=now + lease_duration,
                next_attempt_at=None,
                last_error_code=None,
                updated_at=now,
                started_at=self.started_at or now,
            ),
            recovered,
        )

    def request_cancel(self, *, now: datetime) -> "InvestigationJob":
        if self.is_terminal:
            return self
        if self.status in {
            InvestigationJobStatus.QUEUED,
            InvestigationJobStatus.RETRY_SCHEDULED,
        }:
            return replace(
                self,
                status=InvestigationJobStatus.CANCELLED,
                cancel_requested_at=now,
                completed_at=now,
                lease_token=None,
                lease_expires_at=None,
                updated_at=now,
                version=self.version + 1,
            )
        if self.status is InvestigationJobStatus.RUNNING:
            return replace(
                self,
                status=InvestigationJobStatus.CANCEL_REQUESTED,
                cancel_requested_at=now,
                updated_at=now,
                version=self.version + 1,
            )
        if self.status is InvestigationJobStatus.CANCEL_REQUESTED:
            return self
        raise JobStateTransitionError(self.status, "request cancellation")

    def cancel(self, *, now: datetime) -> "InvestigationJob":
        if self.status is InvestigationJobStatus.CANCELLED:
            return self
        if self.status is not InvestigationJobStatus.CANCEL_REQUESTED:
            raise JobStateTransitionError(self.status, "cancel")
        return replace(
            self,
            status=InvestigationJobStatus.CANCELLED,
            completed_at=now,
            lease_token=None,
            lease_expires_at=None,
            updated_at=now,
            version=self.version + 1,
        )

    def succeed(self, *, now: datetime) -> "InvestigationJob":
        if self.status is not InvestigationJobStatus.RUNNING:
            raise JobStateTransitionError(self.status, "succeed")
        return replace(
            self,
            status=InvestigationJobStatus.SUCCEEDED,
            completed_at=now,
            lease_token=None,
            lease_expires_at=None,
            updated_at=now,
            version=self.version + 1,
        )

    def schedule_retry(
        self,
        *,
        now: datetime,
        retry_at: datetime,
        error_code: str,
    ) -> "InvestigationJob":
        if self.status is not InvestigationJobStatus.RUNNING:
            raise JobStateTransitionError(self.status, "schedule retry")
        if self.attempts >= self.max_attempts:
            return self.fail(now=now, error_code=error_code)
        return replace(
            self,
            status=InvestigationJobStatus.RETRY_SCHEDULED,
            next_attempt_at=retry_at,
            last_error_code=error_code,
            lease_token=None,
            lease_expires_at=None,
            updated_at=now,
            version=self.version + 1,
        )

    def fail(
        self,
        *,
        now: datetime,
        error_code: str,
    ) -> "InvestigationJob":
        if self.status not in {
            InvestigationJobStatus.RUNNING,
            InvestigationJobStatus.CANCEL_REQUESTED,
        }:
            raise JobStateTransitionError(self.status, "fail")
        return replace(
            self,
            status=InvestigationJobStatus.FAILED,
            last_error_code=error_code,
            completed_at=now,
            lease_token=None,
            lease_expires_at=None,
            updated_at=now,
            version=self.version + 1,
        )

    def exhaust(self, *, now: datetime) -> "InvestigationJob":
        if self.is_terminal:
            return self
        return replace(
            self,
            status=InvestigationJobStatus.FAILED,
            last_error_code="attempts_exhausted",
            completed_at=now,
            lease_token=None,
            lease_expires_at=None,
            updated_at=now,
            version=self.version + 1,
        )


@dataclass(frozen=True, slots=True)
class InvestigationJobEvent:
    id: UUID
    tenant_id: UUID
    job_id: UUID
    event_type: InvestigationJobEventType
    from_status: InvestigationJobStatus | None
    to_status: InvestigationJobStatus
    attempt: int
    reason: str
    request_id: str
    trace_id: str
    payload: dict[str, Any]
    created_at: datetime
