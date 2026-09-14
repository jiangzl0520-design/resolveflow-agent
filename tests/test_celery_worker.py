from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from kombu.exceptions import OperationalError

from app.core.config import AppEnvironment, Settings
from app.core.errors import BrokerUnavailableError
from app.domain.auth import Role
from app.domain.investigation_job import (
    InvestigationJob,
    InvestigationJobStatus,
    InvestigationJobType,
)
from app.worker.celery_app import (
    INVESTIGATION_QUEUE,
    PROCESS_INVESTIGATION_JOB_TASK,
    RECOVER_PENDING_INVESTIGATION_JOBS_TASK,
    create_celery_app,
)
from app.worker.celery_dispatcher import (
    CeleryInvestigationTaskDispatcher,
)
from app.worker.tasks import (
    process_investigation_job,
    recover_pending_investigation_jobs,
)
from app.services.investigation_worker_service import (
    WorkerOutcome,
    WorkerOutcomeKind,
)


class RecordingCeleryApplication:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.error = error

    def send_task(self, name: str, **options: object) -> None:
        if self.error is not None:
            raise self.error
        self.calls.append({"name": name, **options})


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": "sqlite+pysqlite:///:memory:",
        "redis_url": "redis://localhost:6379/9",
        "environment": AppEnvironment.TEST,
        "jwt_secret": "test-only-secret-with-more-than-thirty-two-characters",
    }
    values.update(overrides)
    return Settings(**values)


def _job() -> InvestigationJob:
    now = datetime.now(UTC)
    return InvestigationJob(
        id=uuid4(),
        tenant_id=UUID("10000000-0000-0000-0000-000000000001"),
        ticket_id=uuid4(),
        job_type=InvestigationJobType.START_INVESTIGATION,
        status=InvestigationJobStatus.QUEUED,
        actor_id="agent-001",
        actor_roles=frozenset({Role.AGENT}),
        idempotency_key="job-idempotency-key",
        request_hash="a" * 64,
        request_id="request-001",
        trace_id="trace-001",
        attempts=0,
        max_attempts=3,
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


def test_celery_configuration_enforces_at_least_once_delivery_safety() -> None:
    application = create_celery_app(_settings())

    assert application.conf.task_acks_late is True
    assert application.conf.task_reject_on_worker_lost is True
    assert application.conf.worker_prefetch_multiplier == 1
    assert application.conf.task_ignore_result is True
    assert application.conf.result_backend is None
    assert application.conf.broker_transport_options[
        "visibility_timeout"
    ] == 120
    assert application.conf.task_routes[
        PROCESS_INVESTIGATION_JOB_TASK
    ]["queue"] == INVESTIGATION_QUEUE
    assert application.conf.task_routes[
        RECOVER_PENDING_INVESTIGATION_JOBS_TASK
    ]["queue"] == INVESTIGATION_QUEUE
    recovery_schedule = application.conf.beat_schedule[
        "recover-pending-investigation-jobs"
    ]
    assert recovery_schedule["task"] == (
        RECOVER_PENDING_INVESTIGATION_JOBS_TASK
    )
    assert recovery_schedule["schedule"] == 30.0


def test_worker_timing_configuration_requires_safe_ordering() -> None:
    with pytest.raises(ValueError, match="soft limit"):
        _settings(
            task_soft_time_limit_seconds=60,
            task_time_limit_seconds=60,
        )


def test_dispatch_message_contains_only_job_identity_and_trace_headers() -> None:
    application = RecordingCeleryApplication()
    job = _job()

    CeleryInvestigationTaskDispatcher(application).dispatch(job)  # type: ignore[arg-type]

    assert len(application.calls) == 1
    call = application.calls[0]
    assert {key: value for key, value in call.items() if key != "headers"} == {
        "name": PROCESS_INVESTIGATION_JOB_TASK,
        "args": [str(job.id)],
        "task_id": str(job.id),
        "queue": INVESTIGATION_QUEUE,
        "retry": False,
    }
    headers = call["headers"]
    assert headers["request_id"] == job.request_id
    assert headers["trace_id"] == job.trace_id
    assert set(headers) <= {
        "request_id",
        "trace_id",
        "traceparent",
        "tracestate",
    }
    if "traceparent" in headers:
        assert headers["traceparent"].startswith("00-")
    serialized = repr(call)
    assert str(job.tenant_id) not in serialized
    assert job.actor_id not in serialized


def test_broker_error_is_mapped_to_recoverable_application_error() -> None:
    application = RecordingCeleryApplication(
        error=OperationalError("redis unavailable")
    )

    with pytest.raises(BrokerUnavailableError):
        CeleryInvestigationTaskDispatcher(  # type: ignore[arg-type]
            application
        ).dispatch(_job())


def test_celery_task_delegates_to_worker_service_and_closes_runtime(
    monkeypatch,
) -> None:
    job_id = uuid4()

    class WorkerService:
        def __init__(self) -> None:
            self.calls: list[tuple[UUID, str]] = []

        def run(self, current_job_id: UUID, *, worker_id: str):
            self.calls.append((current_job_id, worker_id))
            return WorkerOutcome(WorkerOutcomeKind.SUCCEEDED)

    class Runtime:
        def __init__(self) -> None:
            self.worker_service = WorkerService()
            self.closed = False

        def close(self) -> None:
            self.closed = True

    runtime = Runtime()
    monkeypatch.setattr(
        "app.worker.tasks.create_worker_runtime",
        lambda: runtime,
    )

    process_investigation_job.run(str(job_id))

    assert runtime.worker_service.calls == [(job_id, "unknown-worker")]
    assert runtime.closed is True


def test_recovery_task_runs_pending_dispatch_scan_and_closes_runtime(
    monkeypatch,
) -> None:
    class JobService:
        def __init__(self) -> None:
            self.limits: list[int] = []

        def recover_pending_dispatches(self, *, limit: int) -> int:
            self.limits.append(limit)
            return 3

    class Runtime:
        def __init__(self) -> None:
            self.job_service = JobService()
            self.closed = False

        def close(self) -> None:
            self.closed = True

    runtime = Runtime()
    monkeypatch.setattr(
        "app.worker.tasks.create_recovery_runtime",
        lambda: runtime,
    )

    recover_pending_investigation_jobs.run(limit=7)

    assert runtime.job_service.limits == [7]
    assert runtime.closed is True
