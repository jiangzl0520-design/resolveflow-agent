from datetime import UTC, datetime, timedelta
from os import environ
from urllib.parse import urlparse
from uuid import UUID, uuid4

import pytest

from app.db.database import Database
from app.db.models import TicketRecord
from app.domain.auth import AuthenticatedActor, Role
from app.domain.memory import MemoryDecision, MemorySourceType, MemoryWriteCommand
from app.repositories.authorization_audit_repository import (
    InMemoryAuthorizationAuditRepository,
)
from app.repositories.sqlalchemy_memory_repository import (
    SqlAlchemyMemoryRepository,
)
from app.services.authorization_service import AuthorizationService
from app.services.memory_service import MemoryService
from tests.integration.database_helpers import upgrade_database

TEST_DATABASE_URL = environ.get("TEST_DATABASE_URL")
TENANT_ID = UUID("98000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        TEST_DATABASE_URL is None,
        reason="TEST_DATABASE_URL is not configured",
    ),
]


def test_long_term_memory_is_shared_across_tickets_on_real_postgres(
    request: pytest.FixtureRequest,
) -> None:
    assert TEST_DATABASE_URL is not None
    database_name = urlparse(TEST_DATABASE_URL).path.removeprefix("/")
    assert database_name.endswith("_test")
    upgrade_database(TEST_DATABASE_URL)
    database = Database(TEST_DATABASE_URL)
    request.addfinalizer(database.dispose)
    repository = SqlAlchemyMemoryRepository(database.session_factory)
    service = MemoryService(
        repository,
        AuthorizationService(InMemoryAuthorizationAuditRepository()),
        clock=lambda: NOW,
    )
    identity = uuid4().hex
    subject_id = f"customer-{identity}"
    ticket_ids = (uuid4(), uuid4())
    with database.session_factory() as session:
        session.add_all(
            TicketRecord(
                id=ticket_id,
                tenant_id=TENANT_ID,
                customer_id=subject_id,
                subject="Memory integration",
                description="Cross-ticket preference recall.",
                category="not_received",
                status="open",
                created_at=NOW,
                updated_at=NOW,
                version=1,
            )
            for ticket_id in ticket_ids
        )
        session.commit()
    actor = AuthenticatedActor(
        actor_id="postgres-memory-agent",
        tenant_id=TENANT_ID,
        roles=frozenset({Role.AGENT}),
    )

    result = service.remember(
        actor,
        MemoryWriteCommand(
            subject_id=subject_id,
            memory_key="preference.contact_channel",
            value="in_app",
            source_type=MemorySourceType.EXPLICIT_USER,
            source_reference=f"message-{identity}",
            confidence=0.99,
            observed_at=NOW,
            expires_at=NOW + timedelta(days=30),
            idempotency_key=f"memory-{identity}",
            request_id=f"request-{identity}",
            trace_id=f"trace-{identity}",
        ),
    )

    assert result.decision is MemoryDecision.CREATED
    first = repository.list_active_for_ticket(
        TENANT_ID,
        ticket_ids[0],
        at=NOW,
    )
    second = repository.list_active_for_ticket(
        TENANT_ID,
        ticket_ids[1],
        at=NOW,
    )
    assert len(first) == 1
    assert first == second
    assert first[0].value == "in_app"
