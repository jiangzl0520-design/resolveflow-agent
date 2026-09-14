from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.errors import ConcurrentJobUpdateError
from app.db.sqlalchemy_unit_of_work import (
    SqlAlchemyTicketUnitOfWorkFactory,
)
from app.domain.auth import AuthenticatedActor, Role
from app.domain.investigation_job import (
    InvestigationJobEventType,
    InvestigationJobStatus,
)
from app.services.investigation_executor import (
    InvestigationBootstrapExecutor,
    InvestigationPreparation,
    RetryableInvestigationError,
)
from app.services.investigation_worker_service import (
    InvestigationWorkerService,
    WorkerOutcomeKind,
)
from tests.conftest import TEST_TENANT_ID
from tests.test_tickets import ticket_payload

TENANT_ID = UUID(TEST_TENANT_ID)
ACTOR = AuthenticatedActor(
    actor_id="agent-test-001",
    tenant_id=TENANT_ID,
    roles=frozenset({Role.AGENT}),
)


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class FlakyExecutor:
    def __init__(self, failures: int) -> None:
        self.failures_remaining = failures
        self.calls = 0

    def prepare(self, job):
        self.calls += 1
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise RetryableInvestigationError("temporary_dependency_failure")
        return InvestigationPreparation(summary="dependency recovered")


class CancellingExecutor:
    def __init__(self, cancel_callback) -> None:
        self._cancel_callback = cancel_callback

    def prepare(self, job):
        self._cancel_callback(job.id)
        return InvestigationPreparation(summary="cancel arrived")


def _create_ticket(client: TestClient, customer_id: str = "job-customer"):
    response = client.post(
        "/api/v1/tickets",
        json=ticket_payload(customer_id=customer_id),
        headers={"Idempotency-Key": f"job-ticket-{uuid4()}"},
    )
    assert response.status_code == 201
    return response.json()["data"]


def _submit_job(
    client: TestClient,
    ticket_id: str,
    *,
    key: str | None = None,
    headers: dict[str, str] | None = None,
):
    return client.post(
        f"/api/v1/tickets/{ticket_id}/investigation-jobs",
        headers={
            **(headers or {}),
            "Idempotency-Key": key or f"job-{uuid4()}",
        },
    )


def _worker(application, executor, clock=None):
    database = application.state.database
    return InvestigationWorkerService(
        SqlAlchemyTicketUnitOfWorkFactory(database.session_factory),
        application.state.job_locator,
        executor,
        lease_seconds=90,
        retry_base_seconds=2,
        retry_max_seconds=60,
        clock=clock,
    )


def test_api_accepts_durable_job_and_propagates_trace(
    client: TestClient,
    task_dispatcher,
) -> None:
    ticket = _create_ticket(client)

    response = _submit_job(client, ticket["id"])

    assert response.status_code == 202
    job = response.json()["data"]
    assert job["status"] == "queued"
    assert job["attempts"] == 0
    assert job["trace_id"] == response.headers["X-Trace-ID"]
    assert response.headers["Location"].endswith(job["id"])
    assert response.headers["Dispatch-Pending"] == "false"
    assert response.headers["Idempotency-Replayed"] == "false"
    assert [str(item.id) for item in task_dispatcher.jobs] == [job["id"]]


def test_repeated_submission_replays_one_job_and_one_dispatch(
    client: TestClient,
    task_dispatcher,
) -> None:
    ticket = _create_ticket(client)
    key = f"job-replay-{uuid4()}"

    first = _submit_job(client, ticket["id"], key=key)
    second = _submit_job(client, ticket["id"], key=key)

    assert first.status_code == second.status_code == 202
    assert first.json()["data"]["id"] == second.json()["data"]["id"]
    assert first.headers["Idempotency-Replayed"] == "false"
    assert second.headers["Idempotency-Replayed"] == "true"
    assert len(task_dispatcher.jobs) == 1


def test_same_job_key_with_different_ticket_returns_409(
    client: TestClient,
) -> None:
    first_ticket = _create_ticket(client, "job-first")
    second_ticket = _create_ticket(client, "job-second")
    key = f"job-conflict-{uuid4()}"

    first = _submit_job(client, first_ticket["id"], key=key)
    conflict = _submit_job(client, second_ticket["id"], key=key)

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


def test_broker_failure_keeps_job_pending_and_recovery_dispatches_it(
    client: TestClient,
    application,
    task_dispatcher,
) -> None:
    ticket = _create_ticket(client)
    task_dispatcher.failures_remaining = 1

    response = _submit_job(client, ticket["id"])
    job_id = response.json()["data"]["id"]
    recovered_count = (
        application.state.investigation_job_service
        .recover_pending_dispatches()
    )
    current = client.get(f"/api/v1/investigation-jobs/{job_id}")

    assert response.status_code == 202
    assert response.headers["Dispatch-Pending"] == "true"
    assert response.json()["data"]["last_dispatch_error_code"] == (
        "broker_unavailable"
    )
    assert recovered_count == 1
    assert current.json()["data"]["dispatched_at"] is not None
    assert current.json()["data"]["dispatch_attempts"] == 2
    assert len(task_dispatcher.jobs) == 1


def test_worker_completes_job_and_ticket_change_atomically(
    client: TestClient,
    application,
) -> None:
    ticket = _create_ticket(client)
    job = _submit_job(client, ticket["id"]).json()["data"]
    worker = _worker(application, InvestigationBootstrapExecutor())

    outcome = worker.run(UUID(job["id"]), worker_id="test-worker")
    current_job = client.get(
        f"/api/v1/investigation-jobs/{job['id']}"
    ).json()["data"]
    current_ticket = client.get(
        f"/api/v1/tickets/{ticket['id']}"
    ).json()["data"]
    events = client.get(
        f"/api/v1/investigation-jobs/{job['id']}/events"
    ).json()["data"]

    assert outcome.kind is WorkerOutcomeKind.SUCCEEDED
    assert current_job["status"] == "succeeded"
    assert current_job["attempts"] == 1
    assert current_ticket["status"] == "investigating"
    assert [event["event_type"] for event in events] == [
        "job_created",
        "job_dispatched",
        "job_started",
        "job_succeeded",
    ]


def test_retry_waits_for_backoff_then_succeeds_without_duplicate_effect(
    client: TestClient,
    application,
) -> None:
    ticket = _create_ticket(client)
    job = _submit_job(client, ticket["id"]).json()["data"]
    clock = MutableClock()
    executor = FlakyExecutor(failures=1)
    worker = _worker(application, executor, clock)

    first = worker.run(UUID(job["id"]), worker_id="retry-worker")
    too_early = worker.run(UUID(job["id"]), worker_id="retry-worker")
    clock.advance(2)
    second = worker.run(UUID(job["id"]), worker_id="retry-worker")

    current_job = client.get(
        f"/api/v1/investigation-jobs/{job['id']}"
    ).json()["data"]
    ticket_events = client.get(
        f"/api/v1/tickets/{ticket['id']}/events"
    ).json()["data"]
    job_events = client.get(
        f"/api/v1/investigation-jobs/{job['id']}/events"
    ).json()["data"]

    assert first.kind is WorkerOutcomeKind.RETRY
    assert first.retry_after_seconds == 2
    assert too_early.kind is WorkerOutcomeKind.RETRY
    assert too_early.reason == "retry_not_due"
    assert second.kind is WorkerOutcomeKind.SUCCEEDED
    assert current_job["status"] == "succeeded"
    assert current_job["attempts"] == 2
    assert executor.calls == 2
    assert [
        event["event_type"]
        for event in ticket_events
        if event["event_type"] == "ticket_status_changed"
    ] == ["ticket_status_changed"]
    assert any(
        event["event_type"] == "job_retry_scheduled"
        for event in job_events
    )


def test_retry_exhaustion_becomes_terminal_failure(
    client: TestClient,
    application,
) -> None:
    ticket = _create_ticket(client)
    job = _submit_job(client, ticket["id"]).json()["data"]
    clock = MutableClock()
    worker = _worker(application, FlakyExecutor(failures=10), clock)

    outcomes = []
    for delay in (0, 2, 4):
        clock.advance(delay)
        outcomes.append(
            worker.run(UUID(job["id"]), worker_id="failing-worker")
        )

    current_job = client.get(
        f"/api/v1/investigation-jobs/{job['id']}"
    ).json()["data"]
    current_ticket = client.get(
        f"/api/v1/tickets/{ticket['id']}"
    ).json()["data"]

    assert [outcome.kind for outcome in outcomes] == [
        WorkerOutcomeKind.RETRY,
        WorkerOutcomeKind.RETRY,
        WorkerOutcomeKind.TERMINAL,
    ]
    assert current_job["status"] == "failed"
    assert current_job["attempts"] == 3
    assert current_job["last_error_code"] == "temporary_dependency_failure"
    assert current_ticket["status"] == "open"


def test_queued_cancellation_is_durable_and_worker_is_noop(
    client: TestClient,
    application,
) -> None:
    ticket = _create_ticket(client)
    job = _submit_job(client, ticket["id"]).json()["data"]

    cancelled = client.post(
        f"/api/v1/investigation-jobs/{job['id']}/cancel"
    )
    outcome = _worker(
        application,
        InvestigationBootstrapExecutor(),
    ).run(UUID(job["id"]), worker_id="cancel-worker")
    current_ticket = client.get(
        f"/api/v1/tickets/{ticket['id']}"
    ).json()["data"]

    assert cancelled.status_code == 202
    assert cancelled.json()["data"]["status"] == "cancelled"
    assert outcome.kind is WorkerOutcomeKind.TERMINAL
    assert outcome.reason == "terminal"
    assert current_ticket["status"] == "open"


def test_running_cancellation_invalidates_worker_lease_before_commit(
    client: TestClient,
    application,
) -> None:
    ticket = _create_ticket(client)
    job = _submit_job(client, ticket["id"]).json()["data"]
    service = application.state.investigation_job_service

    executor = CancellingExecutor(
        lambda job_id: service.cancel(
            ACTOR,
            job_id,
            request_id=f"cancel-{uuid4()}",
        )
    )
    outcome = _worker(application, executor).run(
        UUID(job["id"]),
        worker_id="cooperative-worker",
    )

    current_job = client.get(
        f"/api/v1/investigation-jobs/{job['id']}"
    ).json()["data"]
    current_ticket = client.get(
        f"/api/v1/tickets/{ticket['id']}"
    ).json()["data"]
    assert outcome.kind is WorkerOutcomeKind.TERMINAL
    assert current_job["status"] == "cancelled"
    assert current_ticket["status"] == "open"


def test_expired_lease_is_recovered_after_simulated_worker_crash(
    client: TestClient,
    application,
) -> None:
    ticket = _create_ticket(client)
    job = _submit_job(client, ticket["id"]).json()["data"]
    clock = MutableClock()
    database = application.state.database
    uow_factory = SqlAlchemyTicketUnitOfWorkFactory(
        database.session_factory
    )

    with uow_factory(TENANT_ID) as unit_of_work:
        current = unit_of_work.investigation_jobs.get(UUID(job["id"]))
        claimed, _ = current.claim(
            now=clock(),
            lease_token=uuid4(),
            lease_duration=timedelta(seconds=90),
        )
        unit_of_work.investigation_jobs.update(
            claimed,
            expected_version=current.version,
        )
        unit_of_work.commit()

    clock.advance(91)
    outcome = _worker(
        application,
        InvestigationBootstrapExecutor(),
        clock,
    ).run(UUID(job["id"]), worker_id="recovery-worker")
    current_job = client.get(
        f"/api/v1/investigation-jobs/{job['id']}"
    ).json()["data"]
    events = client.get(
        f"/api/v1/investigation-jobs/{job['id']}/events"
    ).json()["data"]

    assert outcome.kind is WorkerOutcomeKind.SUCCEEDED
    assert current_job["attempts"] == 2
    assert any(
        event["event_type"] == "job_lease_recovered"
        for event in events
    )


def test_stale_worker_cannot_commit_after_new_lease_wins(
    client: TestClient,
    application,
) -> None:
    ticket = _create_ticket(client)
    job = _submit_job(client, ticket["id"]).json()["data"]
    clock = MutableClock()
    database = application.state.database
    uow_factory = SqlAlchemyTicketUnitOfWorkFactory(
        database.session_factory
    )

    with uow_factory(TENANT_ID) as unit_of_work:
        current = unit_of_work.investigation_jobs.get(UUID(job["id"]))
        stale_claim, _ = current.claim(
            now=clock(),
            lease_token=uuid4(),
            lease_duration=timedelta(seconds=90),
        )
        unit_of_work.investigation_jobs.update(
            stale_claim,
            expected_version=current.version,
        )
        unit_of_work.commit()

    clock.advance(91)
    _worker(
        application,
        InvestigationBootstrapExecutor(),
        clock,
    ).run(UUID(job["id"]), worker_id="new-worker")

    with pytest.raises(ConcurrentJobUpdateError):
        with uow_factory(TENANT_ID) as unit_of_work:
            unit_of_work.investigation_jobs.update(
                stale_claim.succeed(now=clock()),
                expected_version=stale_claim.version,
            )
            unit_of_work.commit()


def test_customer_cannot_submit_investigation_job(
    client: TestClient,
    auth_headers_factory,
) -> None:
    ticket = _create_ticket(client, customer_id="customer-job-denied")
    customer_headers = auth_headers_factory(
        actor_id="customer-job-denied",
        roles=frozenset({Role.CUSTOMER}),
    )

    response = _submit_job(
        client,
        ticket["id"],
        headers=customer_headers,
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


def test_cross_tenant_job_is_hidden_as_not_found(
    client: TestClient,
    auth_headers_factory,
) -> None:
    ticket = _create_ticket(client, customer_id="tenant-a-job")
    job = _submit_job(client, ticket["id"]).json()["data"]
    tenant_b_headers = auth_headers_factory(
        actor_id="tenant-b-agent",
        tenant_id="10000000-0000-0000-0000-000000000002",
        roles=frozenset({Role.AGENT}),
    )

    response = client.get(
        f"/api/v1/investigation-jobs/{job['id']}",
        headers=tenant_b_headers,
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == (
        "investigation_job_not_found"
    )
