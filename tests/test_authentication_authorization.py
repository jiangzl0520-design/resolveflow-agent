from datetime import timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.config import (
    AppEnvironment,
    DEFAULT_DEVELOPMENT_JWT_SECRET,
    Settings,
)
from app.db.database import Database
from app.db.models import TicketRecord
from app.domain.auth import Role
from app.main import create_app
from app.core.errors import StorageUnavailableError
from app.services.authorization_service import AuthorizationService
from tests.conftest import TEST_TENANT_ID
from tests.integration.database_helpers import migrated_sqlite_url
from tests.test_tickets import ticket_payload

SECOND_TENANT_ID = "10000000-0000-0000-0000-000000000002"


class FailingAuthorizationAuditRepository:
    def add(self, event) -> None:
        raise StorageUnavailableError("audit database unavailable")

    def list_for_tenant(self, tenant_id, *, limit):
        raise StorageUnavailableError("audit database unavailable")


def _create(
    client: TestClient,
    *,
    headers: dict[str, str],
    customer_id: str,
    idempotency_key: str | None = None,
    subject: str = "Tenant-scoped ticket",
):
    return client.post(
        "/api/v1/tickets",
        json=ticket_payload(
            customer_id=customer_id,
            subject=subject,
        ),
        headers={
            **headers,
            "Idempotency-Key": idempotency_key or f"auth-{uuid4()}",
        },
    )


def test_anonymous_and_tampered_tokens_return_unified_401(
    client: TestClient,
    auth_headers_factory,
) -> None:
    anonymous = client.get(
        "/api/v1/tickets/00000000-0000-0000-0000-000000000001",
        headers={"Authorization": ""},
    )
    valid_header = auth_headers_factory()["Authorization"]
    token = valid_header.removeprefix("Bearer ")
    header, payload, signature = token.split(".")
    replacement = "A" if payload[0] != "A" else "B"
    tampered_token = ".".join(
        (header, f"{replacement}{payload[1:]}", signature)
    )
    tampered = client.get(
        "/api/v1/tickets/00000000-0000-0000-0000-000000000001",
        headers={"Authorization": f"Bearer {tampered_token}"},
    )

    for response in (anonymous, tampered):
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "authentication_required"
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert response.json()["meta"]["request_id"] == response.headers[
            "X-Request-ID"
        ]


def test_expired_token_is_rejected(
    client: TestClient,
    application,
) -> None:
    expired = application.state.token_service.issue_access_token(
        actor_id="expired-agent",
        tenant_id=UUID(TEST_TENANT_ID),
        roles=frozenset({Role.AGENT}),
        expires_delta=timedelta(seconds=-1),
    )

    response = client.get(
        "/api/v1/tickets/00000000-0000-0000-0000-000000000001",
        headers={"Authorization": f"Bearer {expired}"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


def test_development_token_endpoint_is_absent_outside_development(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/v1/auth/development-token",
        json={
            "actor_id": "attempted-backdoor",
            "tenant_id": TEST_TENANT_ID,
            "roles": ["tenant_admin"],
        },
        headers={"Authorization": ""},
    )

    assert response.status_code == 404


def test_development_token_endpoint_mints_usable_short_lived_jwt(
    tmp_path,
) -> None:
    database_url = migrated_sqlite_url(tmp_path)
    application = create_app(
        database=Database(database_url),
        settings=Settings(
            database_url=database_url,
            environment=AppEnvironment.DEVELOPMENT,
            jwt_secret=DEFAULT_DEVELOPMENT_JWT_SECRET,
            access_token_expire_minutes=5,
        ),
    )
    with TestClient(application) as development_client:
        token_response = development_client.post(
            "/api/v1/auth/development-token",
            json={
                "actor_id": "development-agent",
                "tenant_id": TEST_TENANT_ID,
                "roles": ["agent"],
            },
        )
        access_token = token_response.json()["access_token"]
        create_response = _create(
            development_client,
            headers={"Authorization": f"Bearer {access_token}"},
            customer_id="development-customer",
        )

    assert token_response.status_code == 200
    assert token_response.json()["token_type"] == "bearer"
    assert token_response.json()["expires_in"] == 300
    assert token_response.headers["Cache-Control"] == "no-store"
    assert create_response.status_code == 201


def test_production_rejects_the_development_jwt_secret() -> None:
    try:
        Settings(
            database_url="sqlite+pysqlite://",
            environment=AppEnvironment.PRODUCTION,
            jwt_secret=DEFAULT_DEVELOPMENT_JWT_SECRET,
        )
    except ValueError as exc:
        assert "Production" in str(exc)
    else:
        raise AssertionError(
            "Production accepted the development JWT secret."
        )


def test_customer_can_only_create_and_read_own_ticket(
    client: TestClient,
    auth_headers_factory,
) -> None:
    owner_headers = auth_headers_factory(
        actor_id="customer-owner",
        roles=frozenset({Role.CUSTOMER}),
    )
    other_headers = auth_headers_factory(
        actor_id="customer-other",
        roles=frozenset({Role.CUSTOMER}),
    )

    created = _create(
        client,
        headers=owner_headers,
        customer_id="customer-owner",
    )
    mismatched_create = _create(
        client,
        headers=owner_headers,
        customer_id="someone-else",
    )
    hidden_from_other_customer = client.get(
        f"/api/v1/tickets/{created.json()['data']['id']}",
        headers=other_headers,
    )
    visible_to_owner = client.get(
        f"/api/v1/tickets/{created.json()['data']['id']}",
        headers=owner_headers,
    )

    assert created.status_code == 201
    assert mismatched_create.status_code == 403
    assert mismatched_create.json()["error"]["code"] == "permission_denied"
    assert hidden_from_other_customer.status_code == 404
    assert visible_to_owner.status_code == 200


def test_customer_cannot_transition_ticket_and_denial_is_audited(
    client: TestClient,
    auth_headers_factory,
) -> None:
    customer_headers = auth_headers_factory(
        actor_id="customer-no-transition",
        roles=frozenset({Role.CUSTOMER}),
    )
    supervisor_headers = auth_headers_factory(
        actor_id="supervisor-audit",
        roles=frozenset({Role.SUPERVISOR}),
    )
    created = _create(
        client,
        headers=customer_headers,
        customer_id="customer-no-transition",
    ).json()["data"]

    denied = client.patch(
        f"/api/v1/tickets/{created['id']}/status",
        json={"target_status": "investigating", "expected_version": 1},
        headers=customer_headers,
    )
    audit_response = client.get(
        "/api/v1/authorization/audit-events",
        headers=supervisor_headers,
    )

    assert denied.status_code == 403
    assert audit_response.status_code == 200
    assert any(
        event["actor_id"] == "customer-no-transition"
        and event["permission"] == "ticket:transition"
        and event["decision"] == "deny"
        and event["reason"] == "role_missing_permission"
        for event in audit_response.json()["data"]
    )


def test_cross_tenant_resource_is_hidden_as_404(
    client: TestClient,
    auth_headers_factory,
) -> None:
    tenant_a = auth_headers_factory(
        actor_id="agent-tenant-a",
        tenant_id=TEST_TENANT_ID,
    )
    tenant_b = auth_headers_factory(
        actor_id="agent-tenant-b",
        tenant_id=SECOND_TENANT_ID,
    )
    created = _create(
        client,
        headers=tenant_b,
        customer_id="customer-in-tenant-b",
    ).json()["data"]

    read_response = client.get(
        f"/api/v1/tickets/{created['id']}",
        headers=tenant_a,
    )
    transition_response = client.patch(
        f"/api/v1/tickets/{created['id']}/status",
        json={"target_status": "investigating", "expected_version": 1},
        headers=tenant_a,
    )
    events_response = client.get(
        f"/api/v1/tickets/{created['id']}/events",
        headers=tenant_a,
    )

    for response in (
        read_response,
        transition_response,
        events_response,
    ):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "ticket_not_found"


def test_same_idempotency_key_is_independent_between_tenants(
    client: TestClient,
    auth_headers_factory,
) -> None:
    shared_key = f"shared-{uuid4()}"
    tenant_a = auth_headers_factory(tenant_id=TEST_TENANT_ID)
    tenant_b = auth_headers_factory(tenant_id=SECOND_TENANT_ID)

    first = _create(
        client,
        headers=tenant_a,
        customer_id="tenant-a-customer",
        idempotency_key=shared_key,
        subject="Tenant A request",
    )
    second = _create(
        client,
        headers=tenant_b,
        customer_id="tenant-b-customer",
        idempotency_key=shared_key,
        subject="Tenant B request",
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["data"]["id"] != second.json()["data"]["id"]


def test_only_privileged_role_can_read_tenant_scoped_audit(
    client: TestClient,
    auth_headers_factory,
) -> None:
    tenant_a_agent = auth_headers_factory(
        actor_id="agent-audit-a",
        tenant_id=TEST_TENANT_ID,
    )
    tenant_b_agent = auth_headers_factory(
        actor_id="agent-audit-b",
        tenant_id=SECOND_TENANT_ID,
    )
    tenant_a_supervisor = auth_headers_factory(
        actor_id="supervisor-a",
        tenant_id=TEST_TENANT_ID,
        roles=frozenset({Role.SUPERVISOR}),
    )
    _create(
        client,
        headers=tenant_a_agent,
        customer_id="audit-a-customer",
    )
    _create(
        client,
        headers=tenant_b_agent,
        customer_id="audit-b-customer",
    )

    agent_denied = client.get(
        "/api/v1/authorization/audit-events",
        headers=tenant_a_agent,
    )
    supervisor_allowed = client.get(
        "/api/v1/authorization/audit-events",
        headers=tenant_a_supervisor,
    )

    assert agent_denied.status_code == 403
    assert supervisor_allowed.status_code == 200
    actor_ids = {
        event["actor_id"]
        for event in supervisor_allowed.json()["data"]
    }
    assert "agent-audit-a" in actor_ids
    assert "agent-audit-b" not in actor_ids


def test_client_cannot_supply_tenant_id_in_ticket_body(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/v1/tickets",
        json=ticket_payload(tenant_id=SECOND_TENANT_ID),
        headers={"Idempotency-Key": f"tenant-injection-{uuid4()}"},
    )

    assert response.status_code == 422
    assert any(
        detail["location"][-1] == "tenant_id"
        for detail in response.json()["error"]["details"]
    )


def test_audit_write_failure_fails_closed_before_ticket_creation(
    tmp_path,
) -> None:
    database_url = migrated_sqlite_url(tmp_path)
    database = Database(database_url)
    application = create_app(
        database=database,
        authorization_service=AuthorizationService(
            FailingAuthorizationAuditRepository()
        ),
        settings=Settings(
            database_url=database_url,
            environment=AppEnvironment.TEST,
            jwt_secret=(
                "audit-failure-test-jwt-secret-at-least-32-characters"
            ),
        ),
    )
    token = application.state.token_service.issue_access_token(
        actor_id="audit-failure-agent",
        tenant_id=UUID(TEST_TENANT_ID),
        roles=frozenset({Role.AGENT}),
    )
    with TestClient(application) as failing_client:
        response = _create(
            failing_client,
            headers={"Authorization": f"Bearer {token}"},
            customer_id="must-not-be-created",
        )
        with database.session_factory() as session:
            ticket_count = session.scalar(
                select(func.count()).select_from(TicketRecord)
            )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storage_unavailable"
    assert ticket_count == 0
    assert "audit database unavailable" not in response.text
