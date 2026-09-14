from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.errors import StorageUnavailableError
from app.db.database import Database
from app.db.sqlalchemy_unit_of_work import SqlAlchemyTicketUnitOfWorkFactory
from app.domain.auth import Role
from app.domain.ticket import Ticket, TicketCategory, TicketStatus
from app.main import create_app
from tests.integration.database_helpers import migrated_sqlite_url
from tests.conftest import TEST_TENANT_ID

TENANT_ID = UUID(TEST_TENANT_ID)


def _auth_headers(application, actor_id: str = "agent-persistence") -> dict[str, str]:
    token = application.state.token_service.issue_access_token(
        actor_id=actor_id,
        tenant_id=TENANT_ID,
        roles=frozenset({Role.AGENT}),
    )
    return {"Authorization": f"Bearer {token}"}


def test_ticket_survives_application_and_session_recreation(
    tmp_path: Path,
) -> None:
    database_url = migrated_sqlite_url(tmp_path)
    first_database = Database(database_url)

    first_application = create_app(database=first_database)
    with TestClient(first_application) as client:
        create_response = client.post(
            "/api/v1/tickets",
            json={
                "customer_id": "customer-persistent",
                "subject": "Persistent ticket",
                "description": "This ticket must survive application recreation.",
                "category": "not_received",
            },
            headers={
                "Idempotency-Key": "persistent-ticket",
                **_auth_headers(first_application),
            },
        )

    assert create_response.status_code == 201
    ticket_id = create_response.json()["data"]["id"]

    second_database = Database(database_url)
    second_application = create_app(database=second_database)
    with TestClient(second_application) as client:
        get_response = client.get(
            f"/api/v1/tickets/{ticket_id}",
            headers=_auth_headers(second_application),
        )

    assert get_response.status_code == 200
    assert get_response.json()["data"]["id"] == ticket_id
    assert get_response.json()["data"]["customer_id"] == "customer-persistent"


def test_failed_write_rolls_back_and_preserves_existing_row(
    tmp_path: Path,
) -> None:
    database_url = migrated_sqlite_url(tmp_path)
    database = Database(database_url)
    unit_of_work_factory = SqlAlchemyTicketUnitOfWorkFactory(
        database.session_factory
    )
    now = datetime.now(UTC)
    ticket = Ticket(
        id=uuid4(),
        tenant_id=TENANT_ID,
        customer_id="customer-rollback",
        subject="Transaction rollback",
        description="A duplicate insert must not damage the original row.",
        category=TicketCategory.OTHER,
        status=TicketStatus.OPEN,
        created_at=now,
        updated_at=now,
    )

    with unit_of_work_factory(TENANT_ID) as unit_of_work:
        unit_of_work.tickets.add(ticket)
        unit_of_work.commit()

    with pytest.raises(StorageUnavailableError):
        with unit_of_work_factory(TENANT_ID) as unit_of_work:
            unit_of_work.tickets.add(ticket)
            unit_of_work.commit()

    with unit_of_work_factory(TENANT_ID) as unit_of_work:
        assert unit_of_work.tickets.get(ticket.id) == ticket
    database.dispose()
