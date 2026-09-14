from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Callable
from uuid import UUID, uuid4

from celery.exceptions import SoftTimeLimitExceeded

from app.core.errors import (
    ConcurrentJobUpdateError,
    ConcurrentTicketUpdateError,
)
from app.domain.investigation_job import (
    InvestigationJob,
    InvestigationJobEventType,
    InvestigationJobStatus,
    JobNotClaimableError,
)
from app.domain.ticket import TicketStatus
from app.domain.ticket_event import (
    TicketActorType,
    TicketEvent,
    TicketEventType,
)
from app.repositories.investigation_job_repository import (
    InvestigationJobLocator,
)
from app.services.investigation_executor import (
    InvestigationExecutor,
    PermanentInvestigationError,
    RetryableInvestigationError,
)
from app.services.investigation_job_service import _job_event
from app.services.unit_of_work import TicketUnitOfWorkFactory


class WorkerOutcomeKind(StrEnum):
    SUCCEEDED = "succeeded"
    RETRY = "retry"
    TERMINAL = "terminal"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class WorkerOutcome:
    kind: WorkerOutcomeKind
    retry_after_seconds: int | None = None
    reason: str | None = None


class InvestigationWorkerService:
    def __init__(
        self,
        unit_of_work_factory: TicketUnitOfWorkFactory,
        job_locator: InvestigationJobLocator,
        executor: InvestigationExecutor,
        *,
        lease_seconds: int,
        retry_base_seconds: int,
        retry_max_seconds: int,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._job_locator = job_locator
        self._executor = executor
        self._lease_duration = timedelta(seconds=lease_seconds)
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds
        self._clock = clock or (lambda: datetime.now(UTC))

    def run(
        self,
        job_id: UUID,
        *,
        worker_id: str,
    ) -> WorkerOutcome:
        location = self._job_locator.locate(job_id)
        if location is None:
            return WorkerOutcome(
                WorkerOutcomeKind.MISSING,
                reason="job_not_found",
            )
        claimed_or_outcome = self._claim(
            location.tenant_id,
            job_id,
            worker_id=worker_id,
        )
        if isinstance(claimed_or_outcome, WorkerOutcome):
            return claimed_or_outcome
        claimed = claimed_or_outcome

        cancellation = self._finalize_cancellation_if_requested(claimed)
        if cancellation is not None:
            return cancellation

        try:
            preparation = self._executor.prepare(claimed)
        except SoftTimeLimitExceeded:
            return self._schedule_retry(
                claimed,
                error_code="worker_soft_timeout",
            )
        except RetryableInvestigationError as exc:
            return self._schedule_retry(
                claimed,
                error_code=exc.error_code,
            )
        except PermanentInvestigationError as exc:
            return self._mark_failed(
                claimed,
                error_code=exc.error_code,
            )
        except Exception:
            return self._schedule_retry(
                claimed,
                error_code="unexpected_worker_error",
            )

        cancellation = self._finalize_cancellation_if_requested(claimed)
        if cancellation is not None:
            return cancellation
        return self._complete(
            claimed,
            worker_id=worker_id,
            preparation_summary=preparation.summary,
        )

    def _claim(
        self,
        tenant_id: UUID,
        job_id: UUID,
        *,
        worker_id: str,
    ) -> InvestigationJob | WorkerOutcome:
        now = self._clock()
        lease_token = uuid4()
        try:
            with self._unit_of_work_factory(tenant_id) as unit_of_work:
                current = unit_of_work.investigation_jobs.get(job_id)
                if current is None:
                    return WorkerOutcome(
                        WorkerOutcomeKind.MISSING,
                        reason="job_not_found",
                    )
                if current.status is InvestigationJobStatus.CANCEL_REQUESTED:
                    cancelled = current.cancel(now=now)
                    unit_of_work.investigation_jobs.update(
                        cancelled,
                        expected_version=current.version,
                    )
                    unit_of_work.investigation_job_events.add(
                        _job_event(
                            cancelled,
                            InvestigationJobEventType.CANCELLED,
                            from_status=current.status,
                            to_status=cancelled.status,
                            reason="worker_observed_cancellation",
                            now=now,
                        )
                    )
                    unit_of_work.commit()
                    return WorkerOutcome(
                        WorkerOutcomeKind.TERMINAL,
                        reason="cancelled",
                    )
                try:
                    claimed, recovered = current.claim(
                        now=now,
                        lease_token=lease_token,
                        lease_duration=self._lease_duration,
                    )
                except JobNotClaimableError as exc:
                    if exc.reason == "attempts_exhausted":
                        exhausted = current.exhaust(now=now)
                        unit_of_work.investigation_jobs.update(
                            exhausted,
                            expected_version=current.version,
                        )
                        unit_of_work.investigation_job_events.add(
                            _job_event(
                                exhausted,
                                InvestigationJobEventType.FAILED,
                                from_status=current.status,
                                to_status=exhausted.status,
                                reason="attempts_exhausted",
                                now=now,
                            )
                        )
                        unit_of_work.commit()
                        return WorkerOutcome(
                            WorkerOutcomeKind.TERMINAL,
                            reason="attempts_exhausted",
                        )
                    if exc.retry_after_seconds is not None:
                        return WorkerOutcome(
                            WorkerOutcomeKind.RETRY,
                            retry_after_seconds=exc.retry_after_seconds,
                            reason=exc.reason,
                        )
                    return WorkerOutcome(
                        WorkerOutcomeKind.TERMINAL,
                        reason=exc.reason,
                    )

                unit_of_work.investigation_jobs.update(
                    claimed,
                    expected_version=current.version,
                )
                unit_of_work.investigation_job_events.add(
                    _job_event(
                        claimed,
                        (
                            InvestigationJobEventType.LEASE_RECOVERED
                            if recovered
                            else InvestigationJobEventType.STARTED
                        ),
                        from_status=current.status,
                        to_status=claimed.status,
                        reason=(
                            "expired_lease_recovered"
                            if recovered
                            else "worker_claimed"
                        ),
                        now=now,
                        payload={
                            "worker_id": worker_id,
                            "lease_token": str(lease_token),
                        },
                    )
                )
                unit_of_work.commit()
                return claimed
        except ConcurrentJobUpdateError:
            return WorkerOutcome(
                WorkerOutcomeKind.RETRY,
                retry_after_seconds=1,
                reason="claim_race",
            )

    def _complete(
        self,
        claimed: InvestigationJob,
        *,
        worker_id: str,
        preparation_summary: str,
    ) -> WorkerOutcome:
        now = self._clock()
        try:
            with self._unit_of_work_factory(
                claimed.tenant_id
            ) as unit_of_work:
                current = unit_of_work.investigation_jobs.get(claimed.id)
                if current is None:
                    return WorkerOutcome(
                        WorkerOutcomeKind.MISSING,
                        reason="job_not_found",
                    )
                if current.status is InvestigationJobStatus.CANCEL_REQUESTED:
                    cancelled = current.cancel(now=now)
                    unit_of_work.investigation_jobs.update(
                        cancelled,
                        expected_version=current.version,
                    )
                    unit_of_work.investigation_job_events.add(
                        _job_event(
                            cancelled,
                            InvestigationJobEventType.CANCELLED,
                            from_status=current.status,
                            to_status=cancelled.status,
                            reason="cancelled_before_commit",
                            now=now,
                        )
                    )
                    unit_of_work.commit()
                    return WorkerOutcome(
                        WorkerOutcomeKind.TERMINAL,
                        reason="cancelled",
                    )
                if not _owns_lease(current, claimed):
                    return self._lease_lost_outcome(current, now)

                ticket = unit_of_work.tickets.get(current.ticket_id)
                if ticket is None:
                    failed = current.fail(
                        now=now,
                        error_code="ticket_not_found",
                    )
                    unit_of_work.investigation_jobs.update(
                        failed,
                        expected_version=current.version,
                    )
                    unit_of_work.investigation_job_events.add(
                        _job_event(
                            failed,
                            InvestigationJobEventType.FAILED,
                            from_status=current.status,
                            to_status=failed.status,
                            reason="ticket_not_found",
                            now=now,
                        )
                    )
                    unit_of_work.commit()
                    return WorkerOutcome(
                        WorkerOutcomeKind.TERMINAL,
                        reason="ticket_not_found",
                    )

                if ticket.status is TicketStatus.OPEN:
                    updated_ticket = ticket.transition_to(
                        TicketStatus.INVESTIGATING,
                        changed_at=now,
                    )
                    unit_of_work.tickets.update(
                        updated_ticket,
                        expected_version=ticket.version,
                    )
                    unit_of_work.events.add(
                        TicketEvent(
                            id=uuid4(),
                            tenant_id=current.tenant_id,
                            ticket_id=ticket.id,
                            event_type=TicketEventType.STATUS_CHANGED,
                            actor_type=TicketActorType.STAFF,
                            actor_id=current.actor_id,
                            payload={
                                "from_status": ticket.status.value,
                                "to_status": updated_ticket.status.value,
                                "from_version": ticket.version,
                                "to_version": updated_ticket.version,
                                "job_id": str(current.id),
                                "worker_id": worker_id,
                            },
                            created_at=now,
                        )
                    )
                elif ticket.status is not TicketStatus.INVESTIGATING:
                    failed = current.fail(
                        now=now,
                        error_code="ticket_state_not_eligible",
                    )
                    unit_of_work.investigation_jobs.update(
                        failed,
                        expected_version=current.version,
                    )
                    unit_of_work.investigation_job_events.add(
                        _job_event(
                            failed,
                            InvestigationJobEventType.FAILED,
                            from_status=current.status,
                            to_status=failed.status,
                            reason="ticket_state_not_eligible",
                            now=now,
                        )
                    )
                    unit_of_work.commit()
                    return WorkerOutcome(
                        WorkerOutcomeKind.TERMINAL,
                        reason="ticket_state_not_eligible",
                    )

                succeeded = current.succeed(now=now)
                unit_of_work.investigation_jobs.update(
                    succeeded,
                    expected_version=current.version,
                )
                unit_of_work.investigation_job_events.add(
                    _job_event(
                        succeeded,
                        InvestigationJobEventType.SUCCEEDED,
                        from_status=current.status,
                        to_status=succeeded.status,
                        reason="investigation_started",
                        now=now,
                        payload={
                            "preparation_summary": preparation_summary,
                            "worker_id": worker_id,
                        },
                    )
                )
                unit_of_work.commit()
                return WorkerOutcome(WorkerOutcomeKind.SUCCEEDED)
        except (ConcurrentJobUpdateError, ConcurrentTicketUpdateError):
            return self._observe_after_fence_loss(claimed)

    def _schedule_retry(
        self,
        claimed: InvestigationJob,
        *,
        error_code: str,
    ) -> WorkerOutcome:
        now = self._clock()
        try:
            with self._unit_of_work_factory(
                claimed.tenant_id
            ) as unit_of_work:
                current = unit_of_work.investigation_jobs.get(claimed.id)
                if current is None:
                    return WorkerOutcome(
                        WorkerOutcomeKind.MISSING,
                        reason="job_not_found",
                    )
                if current.status is InvestigationJobStatus.CANCEL_REQUESTED:
                    cancelled = current.cancel(now=now)
                    unit_of_work.investigation_jobs.update(
                        cancelled,
                        expected_version=current.version,
                    )
                    unit_of_work.investigation_job_events.add(
                        _job_event(
                            cancelled,
                            InvestigationJobEventType.CANCELLED,
                            from_status=current.status,
                            to_status=cancelled.status,
                            reason="cancelled_during_failure",
                            now=now,
                        )
                    )
                    unit_of_work.commit()
                    return WorkerOutcome(
                        WorkerOutcomeKind.TERMINAL,
                        reason="cancelled",
                    )
                if not _owns_lease(current, claimed):
                    return self._lease_lost_outcome(current, now)

                delay = min(
                    self._retry_base_seconds
                    * (2 ** max(0, current.attempts - 1)),
                    self._retry_max_seconds,
                )
                updated = current.schedule_retry(
                    now=now,
                    retry_at=now + timedelta(seconds=delay),
                    error_code=error_code,
                )
                unit_of_work.investigation_jobs.update(
                    updated,
                    expected_version=current.version,
                )
                event_type = (
                    InvestigationJobEventType.FAILED
                    if updated.status is InvestigationJobStatus.FAILED
                    else InvestigationJobEventType.RETRY_SCHEDULED
                )
                unit_of_work.investigation_job_events.add(
                    _job_event(
                        updated,
                        event_type,
                        from_status=current.status,
                        to_status=updated.status,
                        reason=error_code,
                        now=now,
                        payload={"retry_after_seconds": delay},
                    )
                )
                unit_of_work.commit()
                if updated.status is InvestigationJobStatus.FAILED:
                    return WorkerOutcome(
                        WorkerOutcomeKind.TERMINAL,
                        reason=error_code,
                    )
                return WorkerOutcome(
                    WorkerOutcomeKind.RETRY,
                    retry_after_seconds=delay,
                    reason=error_code,
                )
        except ConcurrentJobUpdateError:
            return self._observe_after_fence_loss(claimed)

    def _mark_failed(
        self,
        claimed: InvestigationJob,
        *,
        error_code: str,
    ) -> WorkerOutcome:
        now = self._clock()
        try:
            with self._unit_of_work_factory(
                claimed.tenant_id
            ) as unit_of_work:
                current = unit_of_work.investigation_jobs.get(claimed.id)
                if current is None:
                    return WorkerOutcome(
                        WorkerOutcomeKind.MISSING,
                        reason="job_not_found",
                    )
                if not _owns_lease(current, claimed):
                    return self._lease_lost_outcome(current, now)
                failed = current.fail(now=now, error_code=error_code)
                unit_of_work.investigation_jobs.update(
                    failed,
                    expected_version=current.version,
                )
                unit_of_work.investigation_job_events.add(
                    _job_event(
                        failed,
                        InvestigationJobEventType.FAILED,
                        from_status=current.status,
                        to_status=failed.status,
                        reason=error_code,
                        now=now,
                    )
                )
                unit_of_work.commit()
                return WorkerOutcome(
                    WorkerOutcomeKind.TERMINAL,
                    reason=error_code,
                )
        except ConcurrentJobUpdateError:
            return self._observe_after_fence_loss(claimed)

    def _finalize_cancellation_if_requested(
        self,
        claimed: InvestigationJob,
    ) -> WorkerOutcome | None:
        now = self._clock()
        try:
            with self._unit_of_work_factory(
                claimed.tenant_id
            ) as unit_of_work:
                current = unit_of_work.investigation_jobs.get(claimed.id)
                if current is None:
                    return WorkerOutcome(
                        WorkerOutcomeKind.MISSING,
                        reason="job_not_found",
                    )
                if current.status is InvestigationJobStatus.CANCELLED:
                    return WorkerOutcome(
                        WorkerOutcomeKind.TERMINAL,
                        reason="cancelled",
                    )
                if current.status is not InvestigationJobStatus.CANCEL_REQUESTED:
                    return None
                cancelled = current.cancel(now=now)
                unit_of_work.investigation_jobs.update(
                    cancelled,
                    expected_version=current.version,
                )
                unit_of_work.investigation_job_events.add(
                    _job_event(
                        cancelled,
                        InvestigationJobEventType.CANCELLED,
                        from_status=current.status,
                        to_status=cancelled.status,
                        reason="worker_observed_cancellation",
                        now=now,
                    )
                )
                unit_of_work.commit()
                return WorkerOutcome(
                    WorkerOutcomeKind.TERMINAL,
                    reason="cancelled",
                )
        except ConcurrentJobUpdateError:
            return self._observe_after_fence_loss(claimed)

    def _observe_after_fence_loss(
        self,
        claimed: InvestigationJob,
    ) -> WorkerOutcome:
        with self._unit_of_work_factory(
            claimed.tenant_id
        ) as unit_of_work:
            current = unit_of_work.investigation_jobs.get(claimed.id)
        if current is None:
            return WorkerOutcome(
                WorkerOutcomeKind.MISSING,
                reason="job_not_found",
            )
        return self._lease_lost_outcome(current, self._clock())

    @staticmethod
    def _lease_lost_outcome(
        current: InvestigationJob,
        now: datetime,
    ) -> WorkerOutcome:
        if current.is_terminal:
            return WorkerOutcome(
                WorkerOutcomeKind.TERMINAL,
                reason=current.status.value,
            )
        if current.status is InvestigationJobStatus.CANCEL_REQUESTED:
            return WorkerOutcome(
                WorkerOutcomeKind.RETRY,
                retry_after_seconds=1,
                reason="cancellation_pending",
            )
        retry_after = 1
        if (
            current.lease_expires_at is not None
            and current.lease_expires_at > now
        ):
            retry_after = max(
                1,
                int((current.lease_expires_at - now).total_seconds()),
            )
        return WorkerOutcome(
            WorkerOutcomeKind.RETRY,
            retry_after_seconds=retry_after,
            reason="lease_lost",
        )


def _owns_lease(
    current: InvestigationJob,
    claimed: InvestigationJob,
) -> bool:
    return (
        current.status is InvestigationJobStatus.RUNNING
        and current.lease_token == claimed.lease_token
        and current.version == claimed.version
    )
