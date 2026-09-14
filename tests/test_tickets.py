from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient


def ticket_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "customer_id": "customer-001",
        "subject": "Package not received",
        "description": "The carrier says delivered, but I did not receive it.",
        "category": "not_received",
    }
    payload.update(overrides)
    return payload


def idempotency_headers() -> dict[str, str]:
    return {"Idempotency-Key": f"test-{uuid4()}"}


def test_create_then_get_ticket(client: TestClient) -> None:
    create_response = client.post(
        "/api/v1/tickets",
        json=ticket_payload(),
        headers=idempotency_headers(),
    )

    assert create_response.status_code == 201
    created = create_response.json()["data"]
    UUID(created["id"])
    assert created["status"] == "open"
    assert created["category"] == "not_received"
    assert created["version"] == 1
    assert create_response.json()["meta"]["request_id"]

    get_response = client.get(f"/api/v1/tickets/{created['id']}")

    assert get_response.status_code == 200
    assert get_response.json()["data"] == created


def test_get_missing_ticket_returns_unified_404(client: TestClient) -> None:
    missing_id = "00000000-0000-0000-0000-000000000001"

    response = client.get(f"/api/v1/tickets/{missing_id}")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "ticket_not_found"
    assert missing_id in body["error"]["message"]
    assert body["meta"]["request_id"] == response.headers["X-Request-ID"]


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"subject": "   "}, "subject"),
        ({"category": "invented_category"}, "category"),
        ({"description": "x" * 2001}, "description"),
    ],
)
def test_invalid_ticket_payload_returns_unified_422(
    client: TestClient,
    overrides: dict[str, object],
    field: str,
) -> None:
    response = client.post(
        "/api/v1/tickets",
        json=ticket_payload(**overrides),
        headers=idempotency_headers(),
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert any(
        detail["location"][-1] == field
        for detail in body["error"]["details"]
    )


def test_description_accepts_exact_maximum_length(client: TestClient) -> None:
    response = client.post(
        "/api/v1/tickets",
        json=ticket_payload(description="x" * 2000),
        headers=idempotency_headers(),
    )

    assert response.status_code == 201
    assert len(response.json()["data"]["description"]) == 2000


def test_create_rejects_client_supplied_system_fields(client: TestClient) -> None:
    response = client.post(
        "/api/v1/tickets",
        json=ticket_payload(
            status="resolved",
            id="00000000-0000-0000-0000-000000000001",
            created_at="2020-01-01T00:00:00Z",
        ),
        headers=idempotency_headers(),
    )

    assert response.status_code == 422
    details = response.json()["error"]["details"]
    rejected_fields = {detail["location"][-1] for detail in details}
    assert rejected_fields == {"status", "id", "created_at"}
