from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
from uuid import UUID, uuid4

from app.core.errors import (
    BrokerUnavailableError,
    DuplicateJobIdempotencyKeyError,
    IdempotencyConflictError,
    InvestigationJobNotFoundError,
    StorageUnavailableError,
)
from app.domain.auth import AuthenticatedActor, Permission
from app.domain.investigation_job import (
    InvestigationJob,
    InvestigationJobEvent,
    InvestigationJobEventType,
    InvestigationJobStatus,
    InvestigationJobType,
)
from app.repositories.investigation_job_repository import (
    InvestigationJobLocator,
)
from app.services.authorization_service import AuthorizationService
from app.services.task_dispatcher import InvestigationTaskDispatcher
from app.services.unit_of_work import TicketUnitOfWorkFactory


@dataclass(frozen=True, slots=True)
class SubmitInvestigationJobCommand:
    ticket_id: UUID
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class SubmitInvestigationJobResult:
    job: InvestigationJob
    replayed: bool
    dispatch_pending: bool


class InvestigationJobService:
    def __init__(
        self,
        unit_of_work_factory: TicketUnitOfWorkFactory,
        authorization_service: AuthorizationService,
        dispatcher: InvestigationTaskDispatcher,
        job_locator: InvestigationJobLocator,
        *,
        max_attempts: int,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._authorization = authorization_service
        self._dispatcher = dispatcher
        self._job_locator = job_locator
        self._max_attempts = max_attempts

    def submit(
        self,
        actor: AuthenticatedActor,
        command: SubmitInvestigationJobCommand,
        *,
        request_id: str,
        trace_id: str,
    ) -> SubmitInvestigationJobResult:
        with self._unit_of_work_factory(actor.tenant_id) as unit_of_work:
            ticket = unit_of_work.tickets.get(command.ticket_id)
        self._authorization.require_ticket_access(
            actor,
            Permission.INVESTIGATION_JOB_SUBMIT,
            ticket_id=command.ticket_id,
            ticket=ticket,
            request_id=request_id,
        )

        request_hash = _job_request_hash(command.ticket_id)
        replayed = False
        try:
            job, replayed = self._create_or_replay(
                actor,
                command,
                request_hash=request_hash,
                request_id=request_id,
                trace_id=trace_id,
            )
        except DuplicateJobIdempotencyKeyError:
            job = self._load_replay(
                actor.tenant_id,
                command.idempotency_key,
                request_hash,
            )
            replayed = True

        if job.dispatched_at is None and not job.is_terminal:
            job = self._attempt_dispatch(job)
        return SubmitInvestigationJobResult(
            job=job,
            replayed=replayed,
            dispatch_pending=job.dispatched_at is None and not job.is_terminal,
        )

    def _create_or_replay(
        self,
        actor: AuthenticatedActor,
        command: SubmitInvestigationJobCommand,
        *,
        request_hash: str,
        request_id: str,
        trace_id: str,
    ) -> tuple[InvestigationJob, bool]:
        with self._unit_of_work_factory(actor.tenant_id) as unit_of_work:
            existing = unit_of_work.investigation_jobs.get_by_idempotency(
                InvestigationJobType.START_INVESTIGATION,
                command.idempotency_key,
            )
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise IdempotencyConflictError(command.idempotency_key)
                return existing, True

            now = datetime.now(UTC)
            job = InvestigationJob(
                id=uuid4(),
                tenant_id=actor.tenant_id,
                ticket_id=command.ticket_id,
                job_type=InvestigationJobType.START_INVESTIGATION,
                status=InvestigationJobStatus.QUEUED,
                actor_id=actor.actor_id,
                actor_roles=actor.roles,
                idempotency_key=command.idempotency_key,
                request_hash=request_hash,
                request_id=request_id,
                trace_id=trace_id,
                attempts=0,
                max_attempts=self._max_attempts,
                version=1,
                lease_token=None,
                lease_expires_at=None,
                next_attempt_at=None,
                cancel_requested_at=None,
                dispatched_at=None,
                dispatch_attempts=0,
                last_dispatch_error_code=None,
                last_error_code=None,
                created_at=now,
                updated_at=now,
                started_at=None,
                completed_at=None,
            )
            unit_of_work.investigation_jobs.add(job)
            unit_of_work.investigation_job_events.add(
                _job_event(
                    job,
                    InvestigationJobEventType.CREATED,
                    from_status=None,
                    to_status=job.status,
                    reason="api_accepted",
                    now=now,
                )
            )
            unit_of_work.commit()
            return job, False

    def _load_replay(
        self,
        tenant_id: UUID,
        idempotency_key: str,
        request_hash: str,
    ) -> InvestigationJob:
        with self._unit_of_work_factory(tenant_id) as unit_of_work:
            existing = unit_of_work.investigation_jobs.get_by_idempotency(
                InvestigationJobType.START_INVESTIGATION,
                idempotency_key,
            )
        if existing is None:
            raise StorageUnavailableError(
                "Concurrent job winner could not be loaded."
            )
        if existing.request_hash != request_hash:
            raise IdempotencyConflictError(idempotency_key)
        return existing

    def _attempt_dispatch(
        self,
        job: InvestigationJob,
    ) -> InvestigationJob:
        now = datetime.now(UTC)
        try:
            self._dispatcher.dispatch(job)
        except BrokerUnavailableError:
            dispatched_at = None
            error_code = "broker_unavailable"
            event_type = InvestigationJobEventType.DISPATCH_FAILED
            reason = "broker_unavailable"
        else:
            dispatched_at = now
            error_code = None
            event_type = InvestigationJobEventType.DISPATCHED
            reason = "broker_accepted"

        with self._unit_of_work_factory(job.tenant_id) as unit_of_work:
            current = unit_of_work.investigation_jobs.get(job.id)
            if current is None:
                raise InvestigationJobNotFoundError(job.id)
            unit_of_work.investigation_jobs.record_dispatch_result(
                job.id,
                dispatched_at=dispatched_at,
                error_code=error_code,
            )
            unit_of_work.investigation_job_events.add(
                _job_event(
                    current,
                    event_type,
                    from_status=current.status,
                    to_status=current.status,
                    reason=reason,
                    now=now,
                )
            )
            unit_of_work.commit()
        return self._get_scoped(job.tenant_id, job.id)

    def recover_pending_dispatches(
        self,
        *,
        limit: int = 100,
    ) -> int:
        locations = self._job_locator.list_pending_dispatch(
            now=datetime.now(UTC),
            limit=limit,
        )
        dispatched = 0
        for location in locations:
            job = self._get_scoped(location.tenant_id, location.job_id)
            recovered = self._attempt_dispatch(job)
            if recovered.dispatched_at is not None:
                dispatched += 1
        return dispatched

    def get(
        self,
        actor: AuthenticatedActor,
        job_id: UUID,
        *,
        request_id: str,
    ) -> InvestigationJob:
        with self._unit_of_work_factory(actor.tenant_id) as unit_of_work:
            job = unit_of_work.investigation_jobs.get(job_id)
        return self._authorization.require_investigation_job_access(
            actor,
            Permission.INVESTIGATION_JOB_READ,
            job_id=job_id,
            job=job,
            request_id=request_id,
        )

    def cancel(
        self,
        actor: AuthenticatedActor,
        job_id: UUID,
        *,
        request_id: str,
    ) -> InvestigationJob:
        with self._unit_of_work_factory(actor.tenant_id) as unit_of_work:
            job = unit_of_work.investigation_jobs.get(job_id)
        self._authorization.require_investigation_job_access(
            actor,
            Permission.INVESTIGATION_JOB_CANCEL,
            job_id=job_id,
            job=job,
            request_id=request_id,
        )

        with self._unit_of_work_factory(actor.tenant_id) as unit_of_work:
            current = unit_of_work.investigation_jobs.get(job_id)
            if current is None:
                raise InvestigationJobNotFoundError(job_id)
            updated = current.request_cancel(now=datetime.now(UTC))
            if updated is current:
                return current
            unit_of_work.investigation_jobs.update(
                updated,
                expected_version=current.version,
            )
            event_type = (
                InvestigationJobEventType.CANCELLED
                if updated.status is InvestigationJobStatus.CANCELLED
                else InvestigationJobEventType.CANCEL_REQUESTED
            )
            unit_of_work.investigation_job_events.add(
                _job_event(
                    updated,
                    event_type,
                    from_status=current.status,
                    to_status=updated.status,
                    reason="cancelled_by_user",
                    now=updated.updated_at,
                )
            )
            unit_of_work.commit()
            return updated

    def list_events(
        self,
        actor: AuthenticatedActor,
        job_id: UUID,
        *,
        request_id: str,
    ) -> list[InvestigationJobEvent]:
        with self._unit_of_work_factory(actor.tenant_id) as unit_of_work:
            job = unit_of_work.investigation_jobs.get(job_id)
        self._authorization.require_investigation_job_access(
            actor,
            Permission.INVESTIGATION_JOB_EVENTS_READ,
            job_id=job_id,
            job=job,
            request_id=request_id,
        )
        with self._unit_of_work_factory(actor.tenant_id) as unit_of_work:
            return unit_of_work.investigation_job_events.list_for_job(job_id)

    def _get_scoped(
        self,
        tenant_id: UUID,
        job_id: UUID,
    ) -> InvestigationJob:
        with self._unit_of_work_factory(tenant_id) as unit_of_work:
            job = unit_of_work.investigation_jobs.get(job_id)
        if job is None:
            raise InvestigationJobNotFoundError(job_id)
        return job


def _job_request_hash(ticket_id: UUID) -> str:
    payload = json.dumps(
        {
            "ticket_id": str(ticket_id),
            "job_type": InvestigationJobType.START_INVESTIGATION.value,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _job_event(
    job: InvestigationJob,
    event_type: InvestigationJobEventType,
    *,
    from_status: InvestigationJobStatus | None,
    to_status: InvestigationJobStatus,
    reason: str,
    now: datetime,
    payload: dict[str, object] | None = None,
) -> InvestigationJobEvent:
    return InvestigationJobEvent(
        id=uuid4(),
        tenant_id=job.tenant_id,
        job_id=job.id,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        attempt=job.attempts,
        reason=reason,
        request_id=job.request_id,
        trace_id=job.trace_id,
        payload=dict(payload or {}),
        created_at=now,
    )
