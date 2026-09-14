from uuid import uuid4

from fastapi.testclient import TestClient

from tests.test_tickets import ticket_payload


def test_repeated_create_returns_one_ticket_and_one_audit_event(
    client: TestClient,
) -> None:
    headers = {"Idempotency-Key": f"repeat-{uuid4()}"}

    first = client.post(
        "/api/v1/tickets",
        json=ticket_payload(),
        headers=headers,
    )
    second = client.post(
        "/api/v1/tickets",
        json=ticket_payload(),
        headers=headers,
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["data"]["id"] == second.json()["data"]["id"]
    assert first.headers["Idempotency-Replayed"] == "false"
    assert second.headers["Idempotency-Replayed"] == "true"

    ticket_id = first.json()["data"]["id"]
    events = client.get(f"/api/v1/tickets/{ticket_id}/events")
    assert events.status_code == 200
    assert [event["event_type"] for event in events.json()["data"]] == [
        "ticket_created"
    ]


def test_same_idempotency_key_with_different_input_returns_409(
    client: TestClient,
) -> None:
    headers = {"Idempotency-Key": f"conflict-{uuid4()}"}
    first = client.post(
        "/api/v1/tickets",
        json=ticket_payload(),
        headers=headers,
    )

    conflict = client.post(
        "/api/v1/tickets",
        json=ticket_payload(subject="A different business request"),
        headers=headers,
    )

    assert first.status_code == 201
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


def test_create_requires_idempotency_key(client: TestClient) -> None:
    response = client.post("/api/v1/tickets", json=ticket_payload())

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert any(
        detail["location"][-1] == "Idempotency-Key"
        for detail in response.json()["error"]["details"]
    )


def test_valid_status_transition_increments_version_and_writes_event(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/v1/tickets",
        json=ticket_payload(),
        headers={"Idempotency-Key": f"transition-{uuid4()}"},
    ).json()["data"]

    response = client.patch(
        f"/api/v1/tickets/{created['id']}/status",
        json={"target_status": "investigating", "expected_version": 1},
    )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "investigating"
    assert response.json()["data"]["version"] == 2

    events = client.get(
        f"/api/v1/tickets/{created['id']}/events"
    ).json()["data"]
    assert [event["event_type"] for event in events] == [
        "ticket_created",
        "ticket_status_changed",
    ]
    assert events[-1]["payload"] == {
        "from_status": "open",
        "to_status": "investigating",
        "from_version": 1,
        "to_version": 2,
    }


def test_invalid_transition_is_rejected_without_event(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/v1/tickets",
        json=ticket_payload(),
        headers={"Idempotency-Key": f"invalid-transition-{uuid4()}"},
    ).json()["data"]

    response = client.patch(
        f"/api/v1/tickets/{created['id']}/status",
        json={"target_status": "resolved", "expected_version": 1},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_ticket_transition"
    current = client.get(f"/api/v1/tickets/{created['id']}").json()["data"]
    assert current["status"] == "open"
    assert current["version"] == 1
    events = client.get(
        f"/api/v1/tickets/{created['id']}/events"
    ).json()["data"]
    assert len(events) == 1


def test_stale_version_is_rejected_without_overwriting_new_state(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/v1/tickets",
        json=ticket_payload(),
        headers={"Idempotency-Key": f"stale-version-{uuid4()}"},
    ).json()["data"]
    first_update = client.patch(
        f"/api/v1/tickets/{created['id']}/status",
        json={"target_status": "investigating", "expected_version": 1},
    )
    assert first_update.status_code == 200

    stale_update = client.patch(
        f"/api/v1/tickets/{created['id']}/status",
        json={"target_status": "waiting_for_customer", "expected_version": 1},
    )

    assert stale_update.status_code == 409
    assert stale_update.json()["error"]["code"] == "concurrent_ticket_update"
    current = client.get(f"/api/v1/tickets/{created['id']}").json()["data"]
    assert current["status"] == "investigating"
    assert current["version"] == 2
    events = client.get(
        f"/api/v1/tickets/{created['id']}/events"
    ).json()["data"]
    assert len(events) == 2
