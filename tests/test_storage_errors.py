from fastapi.testclient import TestClient
from uuid import UUID

from app.core.errors import StorageUnavailableError
from app.domain.auth import Role
from app.main import create_app
from tests.conftest import TEST_TENANT_ID


class FailingIdempotencyRepository:
    def get(self, operation: str, key: str):
        raise StorageUnavailableError("database query failed")


class FailingUnitOfWork:
    idempotency = FailingIdempotencyRepository()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None


class FailingUnitOfWorkFactory:
    def __call__(self, tenant_id) -> FailingUnitOfWork:
        return FailingUnitOfWork()


def test_storage_failure_returns_safe_503_response() -> None:
    application = create_app(
        unit_of_work_factory=FailingUnitOfWorkFactory()
    )
    token = application.state.token_service.issue_access_token(
        actor_id="agent-storage",
        tenant_id=UUID(TEST_TENANT_ID),
        roles=frozenset({Role.AGENT}),
    )
    with TestClient(application) as client:
        response = client.post(
            "/api/v1/tickets",
            json={
                "customer_id": "customer-503",
                "subject": "Database unavailable",
                "description": "The response must not leak database details.",
                "category": "other",
            },
            headers={
                "Idempotency-Key": "storage-failure",
                "Authorization": f"Bearer {token}",
            },
        )

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "storage_unavailable",
        "message": "Persistent storage is temporarily unavailable.",
        "details": [],
    }
    assert response.json()["meta"]["request_id"] == response.headers[
        "X-Request-ID"
    ]
    assert "database query failed" not in response.text
