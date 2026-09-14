from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from os import environ
from threading import Barrier, RLock
from urllib.parse import urlparse
from uuid import UUID, uuid4

import pytest

from app.core.errors import ConcurrentTicketUpdateError, TicketNotFoundError
from app.db.database import Database
from app.db.sqlalchemy_unit_of_work import SqlAlchemyTicketUnitOfWorkFactory
from app.domain.auth import AuthenticatedActor, Role
from app.domain.investigation_job import InvestigationJobStatus
from app.domain.model_call import ModelCallRecord, ModelCallStatus
from app.domain.knowledge import IngestPolicyDocumentCommand
from app.domain.ticket import Ticket, TicketCategory, TicketStatus
from app.repositories.authorization_audit_repository import (
    InMemoryAuthorizationAuditRepository,
)
from app.repositories.sqlalchemy_authorization_audit_repository import (
    SqlAlchemyAuthorizationAuditRepository,
)
from app.repositories.sqlalchemy_investigation_job_repository import (
    SqlAlchemyInvestigationJobLocator,
)
from app.repositories.sqlalchemy_model_call_repository import (
    SqlAlchemyModelCallRepository,
)
from app.repositories.sqlalchemy_knowledge_repository import (
    SqlAlchemyKnowledgeRepository,
)
from app.services.authorization_service import AuthorizationService
from app.services.investigation_executor import (
    InvestigationBootstrapExecutor,
)
from app.services.investigation_job_service import (
    InvestigationJobService,
    SubmitInvestigationJobCommand,
)
from app.services.investigation_worker_service import (
    InvestigationWorkerService,
    WorkerOutcomeKind,
)
from app.services.knowledge_ingestion_service import (
    KnowledgeIngestionService,
)
from app.services.ticket_service import (
    CreateTicketCommand,
    TicketService,
    TransitionTicketStatusCommand,
)
from tests.integration.database_helpers import upgrade_database

TEST_DATABASE_URL = environ.get("TEST_DATABASE_URL")
TENANT_ID = UUID("20000000-0000-0000-0000-000000000001")
ACTOR = AuthenticatedActor(
    actor_id="postgres-agent",
    tenant_id=TENANT_ID,
    roles=frozenset({Role.AGENT}),
)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        TEST_DATABASE_URL is None,
        reason="TEST_DATABASE_URL is not configured",
    ),
]


def _postgres_unit_of_work_factory():
    assert TEST_DATABASE_URL is not None
    database_name = urlparse(TEST_DATABASE_URL).path.removeprefix("/")
    assert database_name.endswith("_test"), (
        "Real PostgreSQL tests require a dedicated *_test database."
    )
    upgrade_database(TEST_DATABASE_URL)
    database = Database(TEST_DATABASE_URL)
    return database, SqlAlchemyTicketUnitOfWorkFactory(
        database.session_factory
    )


def _ticket_service(unit_of_work_factory) -> TicketService:
    return TicketService(
        unit_of_work_factory,
        AuthorizationService(InMemoryAuthorizationAuditRepository()),
    )


class RecordingDispatcher:
    def __init__(self) -> None:
        self._lock = RLock()
        self.job_ids: list[UUID] = []

    def dispatch(self, job) -> None:
        with self._lock:
            self.job_ids.append(job.id)


def test_real_postgres_migration_and_ticket_round_trip() -> None:
    database, unit_of_work_factory = _postgres_unit_of_work_factory()
    now = datetime.now(UTC)
    ticket = Ticket(
        id=uuid4(),
        tenant_id=TENANT_ID,
        customer_id="postgres-integration",
        subject="Real PostgreSQL round trip",
        description="This row proves the PostgreSQL adapter really executed.",
        category=TicketCategory.OTHER,
        status=TicketStatus.OPEN,
        created_at=now,
        updated_at=now,
    )

    with unit_of_work_factory(TENANT_ID) as unit_of_work:
        unit_of_work.tickets.add(ticket)
        unit_of_work.commit()

    with unit_of_work_factory(TENANT_ID) as unit_of_work:
        assert unit_of_work.tickets.get(ticket.id) == ticket
    assert database.engine.dialect.name == "postgresql"
    database.dispose()


def test_real_postgres_concurrent_duplicate_create_is_idempotent() -> None:
    database, unit_of_work_factory = _postgres_unit_of_work_factory()
    command = CreateTicketCommand(
        customer_id="postgres-concurrent-idempotency",
        subject="Concurrent duplicate create",
        description="Two simultaneous retries must create one ticket.",
        category=TicketCategory.OTHER,
        idempotency_key=f"postgres-concurrent-{uuid4()}",
    )
    barrier = Barrier(2)
    authorization = AuthorizationService(
        InMemoryAuthorizationAuditRepository()
    )

    def create_once():
        barrier.wait()
        return TicketService(
            unit_of_work_factory,
            authorization,
        ).create_ticket(
            ACTOR,
            command,
            request_id=f"postgres-create-{uuid4()}",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [
            future.result()
            for future in [executor.submit(create_once) for _ in range(2)]
        ]

    assert len({result.ticket.id for result in results}) == 1
    assert sorted(result.replayed for result in results) == [False, True]
    ticket_id = results[0].ticket.id
    with unit_of_work_factory(TENANT_ID) as unit_of_work:
        assert len(unit_of_work.events.list_for_ticket(ticket_id)) == 1
    database.dispose()


def test_real_postgres_concurrent_updates_allow_one_winner() -> None:
    database, unit_of_work_factory = _postgres_unit_of_work_factory()
    service = _ticket_service(unit_of_work_factory)
    created = service.create_ticket(
        ACTOR,
        CreateTicketCommand(
            customer_id="postgres-optimistic-lock",
            subject="Concurrent status updates",
            description="Only one update may consume version one.",
            category=TicketCategory.OTHER,
            idempotency_key=f"postgres-version-{uuid4()}",
        ),
        request_id=f"postgres-version-create-{uuid4()}",
    ).ticket
    command = TransitionTicketStatusCommand(
        ticket_id=created.id,
        target_status=TicketStatus.INVESTIGATING,
        expected_version=1,
    )
    barrier = Barrier(2)

    def update_once():
        barrier.wait()
        try:
            return _ticket_service(unit_of_work_factory).transition_status(
                ACTOR,
                command,
                request_id=f"postgres-update-{uuid4()}",
            )
        except ConcurrentTicketUpdateError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [
            future.result()
            for future in [executor.submit(update_once) for _ in range(2)]
        ]

    assert sum(isinstance(outcome, Ticket) for outcome in outcomes) == 1
    assert sum(
        isinstance(outcome, ConcurrentTicketUpdateError)
        for outcome in outcomes
    ) == 1
    current = service.get_ticket(
        ACTOR,
        created.id,
        request_id=f"postgres-get-{uuid4()}",
    )
    assert current.status == TicketStatus.INVESTIGATING
    assert current.version == 2
    assert len(
        service.list_events(
            ACTOR,
            created.id,
            request_id=f"postgres-events-{uuid4()}",
        )
    ) == 2
    database.dispose()


def test_real_postgres_hides_cross_tenant_ticket_and_audits_denial() -> None:
    database, unit_of_work_factory = _postgres_unit_of_work_factory()
    audit_repository = SqlAlchemyAuthorizationAuditRepository(
        database.session_factory
    )
    authorization = AuthorizationService(audit_repository)
    service = TicketService(unit_of_work_factory, authorization)
    tenant_b = UUID("20000000-0000-0000-0000-000000000002")
    actor_b = AuthenticatedActor(
        actor_id=f"postgres-tenant-b-{uuid4()}",
        tenant_id=tenant_b,
        roles=frozenset({Role.AGENT}),
    )
    created = service.create_ticket(
        ACTOR,
        CreateTicketCommand(
            customer_id="postgres-tenant-a-customer",
            subject="Cross-tenant isolation",
            description="Tenant B must never load this ticket.",
            category=TicketCategory.OTHER,
            idempotency_key=f"postgres-tenant-a-{uuid4()}",
        ),
        request_id=f"postgres-tenant-create-{uuid4()}",
    ).ticket
    denied_request_id = f"postgres-tenant-deny-{uuid4()}"

    with pytest.raises(TicketNotFoundError):
        service.get_ticket(
            actor_b,
            created.id,
            request_id=denied_request_id,
        )

    with unit_of_work_factory(tenant_b) as unit_of_work:
        assert unit_of_work.tickets.get(created.id) is None
    tenant_b_audit = audit_repository.list_for_tenant(
        tenant_b,
        limit=20,
    )
    assert any(
        event.request_id == denied_request_id
        and event.decision.value == "deny"
        and event.reason == "not_found_or_out_of_tenant_scope"
        for event in tenant_b_audit
    )
    database.dispose()


def test_real_postgres_scopes_idempotency_key_by_tenant() -> None:
    database, unit_of_work_factory = _postgres_unit_of_work_factory()
    authorization = AuthorizationService(
        SqlAlchemyAuthorizationAuditRepository(database.session_factory)
    )
    service = TicketService(unit_of_work_factory, authorization)
    tenant_b = UUID("20000000-0000-0000-0000-000000000003")
    actor_b = AuthenticatedActor(
        actor_id="postgres-tenant-b-agent",
        tenant_id=tenant_b,
        roles=frozenset({Role.AGENT}),
    )
    shared_key = f"postgres-cross-tenant-key-{uuid4()}"

    tenant_a_ticket = service.create_ticket(
        ACTOR,
        CreateTicketCommand(
            customer_id="postgres-idem-tenant-a",
            subject="Tenant A",
            description="Same key, different tenant.",
            category=TicketCategory.OTHER,
            idempotency_key=shared_key,
        ),
        request_id=f"postgres-idem-a-{uuid4()}",
    ).ticket
    tenant_b_ticket = service.create_ticket(
        actor_b,
        CreateTicketCommand(
            customer_id="postgres-idem-tenant-b",
            subject="Tenant B",
            description="Same key, different tenant.",
            category=TicketCategory.OTHER,
            idempotency_key=shared_key,
        ),
        request_id=f"postgres-idem-b-{uuid4()}",
    ).ticket

    assert tenant_a_ticket.id != tenant_b_ticket.id
    assert tenant_a_ticket.tenant_id == TENANT_ID
    assert tenant_b_ticket.tenant_id == tenant_b
    database.dispose()


def test_real_postgres_concurrent_workers_commit_side_effect_once() -> None:
    database, unit_of_work_factory = _postgres_unit_of_work_factory()
    authorization = AuthorizationService(
        InMemoryAuthorizationAuditRepository()
    )
    ticket = TicketService(
        unit_of_work_factory,
        authorization,
    ).create_ticket(
        ACTOR,
        CreateTicketCommand(
            customer_id="postgres-worker-race",
            subject="Concurrent worker claim",
            description="Only the lease winner may commit the transition.",
            category=TicketCategory.OTHER,
            idempotency_key=f"postgres-worker-ticket-{uuid4()}",
        ),
        request_id=f"postgres-worker-ticket-request-{uuid4()}",
    ).ticket
    locator = SqlAlchemyInvestigationJobLocator(database.session_factory)
    job = InvestigationJobService(
        unit_of_work_factory,
        authorization,
        RecordingDispatcher(),
        locator,
        max_attempts=3,
    ).submit(
        ACTOR,
        SubmitInvestigationJobCommand(
            ticket_id=ticket.id,
            idempotency_key=f"postgres-worker-job-{uuid4()}",
        ),
        request_id=f"postgres-worker-job-request-{uuid4()}",
        trace_id=f"postgres-worker-trace-{uuid4()}",
    ).job
    barrier = Barrier(2)

    def run_once(worker_id: str):
        barrier.wait()
        return InvestigationWorkerService(
            unit_of_work_factory,
            locator,
            InvestigationBootstrapExecutor(),
            lease_seconds=90,
            retry_base_seconds=2,
            retry_max_seconds=60,
        ).run(job.id, worker_id=worker_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [
            future.result()
            for future in [
                executor.submit(run_once, "postgres-worker-a"),
                executor.submit(run_once, "postgres-worker-b"),
            ]
        ]

    with unit_of_work_factory(TENANT_ID) as unit_of_work:
        current_job = unit_of_work.investigation_jobs.get(job.id)
        current_ticket = unit_of_work.tickets.get(ticket.id)
        ticket_events = unit_of_work.events.list_for_ticket(ticket.id)

    assert current_job is not None
    assert current_job.status is InvestigationJobStatus.SUCCEEDED
    assert current_job.attempts == 1
    assert current_ticket is not None
    assert current_ticket.status is TicketStatus.INVESTIGATING
    assert sum(
        event.payload.get("job_id") == str(job.id)
        for event in ticket_events
    ) == 1
    assert sum(
        outcome.kind is WorkerOutcomeKind.SUCCEEDED
        for outcome in outcomes
    ) == 1
    database.dispose()


def test_real_postgres_persists_model_call_trace_metadata() -> None:
    database, _ = _postgres_unit_of_work_factory()
    repository = SqlAlchemyModelCallRepository(database.session_factory)
    trace_id = f"postgres-model-trace-{uuid4()}"
    now = datetime.now(UTC)
    record = ModelCallRecord(
        id=uuid4(),
        tenant_id=TENANT_ID,
        operation="triage_investigation_ticket",
        provider="fake",
        model="fake-model",
        prompt_name="ticket-investigation-triage",
        prompt_version="1.0.0",
        prompt_hash="b" * 64,
        response_schema_name="InvestigationTriage",
        response_schema_hash="c" * 64,
        resource_type="ticket",
        resource_id=str(uuid4()),
        status=ModelCallStatus.SUCCEEDED,
        attempts=2,
        attempt_error_codes=("provider_timeout",),
        latency_ms=15.25,
        input_tokens=90,
        output_tokens=24,
        provider_request_id="postgres-provider-request",
        provider_response_id="postgres-provider-response",
        error_code=None,
        request_id=f"postgres-model-request-{uuid4()}",
        trace_id=trace_id,
        started_at=now,
        completed_at=now,
    )

    repository.add(record)

    assert repository.list_for_trace(TENANT_ID, trace_id) == [record]
    assert repository.list_for_trace(
        UUID("20000000-0000-0000-0000-000000000099"),
        trace_id,
    ) == []
    database.dispose()


def test_real_postgres_persists_traceable_knowledge_chunks() -> None:
    database, _ = _postgres_unit_of_work_factory()
    repository = SqlAlchemyKnowledgeRepository(database.session_factory)
    unique = uuid4()
    source_key = f"policy/postgres/{unique}"
    now = datetime.now(UTC)
    command = IngestPolicyDocumentCommand(
        tenant_id=TENANT_ID,
        source_key=source_key,
        document_version="1.0.0",
        title="PostgreSQL配送政策",
        source_uri=f"repo://policies/{unique}.md",
        source_text=(
            "# 配送政策\n\n## 签收争议\n\n"
            "显示签收但客户否认收货时，必须查询签收证明。\n\n"
            "## 退款限制\n\n证据冲突必须转人工。"
        ),
        effective_from=now,
        idempotency_key=f"postgres-knowledge-{unique}",
        request_id=f"postgres-knowledge-request-{unique}",
        trace_id=f"postgres-knowledge-trace-{unique}",
    )

    result = KnowledgeIngestionService(repository).ingest(command)
    assert result.document is not None
    reloaded = repository.find_document_by_source_version(
        TENANT_ID,
        source_key,
        "1.0.0",
    )
    chunks = repository.list_chunks(TENANT_ID, result.document.id)

    assert database.engine.dialect.name == "postgresql"
    assert reloaded == result.document
    assert len(chunks) == 2
    assert chunks[0].section_path == ("配送政策", "签收争议")
    assert chunks[0].source_uri == result.document.source_uri
    assert chunks[0].document_version == "1.0.0"
    assert chunks[0].effective_from == result.document.effective_from
    assert repository.list_chunks(
        UUID("20000000-0000-0000-0000-000000000099"),
        result.document.id,
    ) == []
    database.dispose()
