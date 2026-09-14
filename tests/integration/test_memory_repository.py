from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select

from app.db.database import Database
from app.db.models import LongTermMemoryRecord, MemoryAuditEventRecord, TicketRecord
from app.domain.auth import AuthenticatedActor, Role
from app.domain.memory import (
    MemoryDecision,
    MemoryDeleteCommand,
    MemorySourceType,
    MemoryWriteCommand,
)
from app.repositories.authorization_audit_repository import (
    InMemoryAuthorizationAuditRepository,
)
from app.repositories.sqlalchemy_memory_repository import (
    SqlAlchemyMemoryRepository,
)
from app.services.authorization_service import AuthorizationService
from app.services.memory_service import MemoryService
from tests.integration.database_helpers import migrated_sqlite_url

TENANT_ID = UUID("97000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 8, 5, 11, 0, tzinfo=UTC)


def test_sql_memory_lifecycle_and_ticket_scoped_recall(tmp_path: Path) -> None:
    database = Database(migrated_sqlite_url(tmp_path))
    repository = SqlAlchemyMemoryRepository(database.session_factory)
    service = MemoryService(
        repository,
        AuthorizationService(InMemoryAuthorizationAuditRepository()),
        clock=lambda: NOW,
    )
    actor = AuthenticatedActor(
        actor_id="sql-memory-supervisor",
        tenant_id=TENANT_ID,
        roles=frozenset({Role.SUPERVISOR}),
    )
    ticket_id = uuid4()
    with database.session_factory() as session:
        session.add(
            TicketRecord(
                id=ticket_id,
                tenant_id=TENANT_ID,
                customer_id="customer-sql",
                subject="Delivery dispute",
                description="Package not received.",
                category="not_received",
                status="open",
                created_at=NOW,
                updated_at=NOW,
                version=1,
            )
        )
        session.commit()

    created = service.remember(
        actor,
        MemoryWriteCommand(
            subject_id="customer-sql",
            memory_key="preference.response_style",
            value="concise",
            source_type=MemorySourceType.EXPLICIT_USER,
            source_reference="message-sql-1",
            confidence=0.99,
            observed_at=NOW,
            expires_at=NOW + timedelta(days=30),
            idempotency_key="sql-memory-create",
            request_id="request-sql-memory-create",
            trace_id="trace-sql-memory",
        ),
    )

    assert created.decision is MemoryDecision.CREATED
    recalled = repository.list_active_for_ticket(
        TENANT_ID,
        ticket_id,
        at=NOW,
    )
    assert len(recalled) == 1
    assert recalled[0].value == "concise"

    deleted = service.forget(
        actor,
        MemoryDeleteCommand(
            subject_id="customer-sql",
            memory_key="preference.response_style",
            reason="Customer requested deletion.",
            idempotency_key="sql-memory-delete",
            request_id="request-sql-memory-delete",
            trace_id="trace-sql-memory",
        ),
    )

    assert deleted.decision is MemoryDecision.DELETED
    assert repository.list_active_for_ticket(
        TENANT_ID,
        ticket_id,
        at=NOW,
    ) == ()
    with database.session_factory() as session:
        stored = session.scalar(select(LongTermMemoryRecord))
        events = session.scalars(select(MemoryAuditEventRecord)).all()
    assert stored is not None
    assert stored.value is None
    assert len(stored.value_hash) == 64
    assert {item.decision for item in events} == {"created", "deleted"}
    assert all(len(item.candidate_hash) == 64 for item in events)
    database.dispose()
