from datetime import UTC, datetime, timedelta

from app.domain.auth import Role

# Keep all payloads in this module stable while preventing the API test from
# expiring merely because the calendar moved past a hard-coded fixture date.
NOW = datetime.now(UTC)


def _payload(**overrides):
    values = {
        "subject_id": "customer-memory-api",
        "memory_key": "preference.language",
        "value": "zh-CN",
        "source_type": "explicit_user",
        "source_reference": "message-api-001",
        "confidence": 0.99,
        "observed_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(days=30)).isoformat(),
    }
    values.update(overrides)
    return values


def test_memory_api_create_recall_and_supervisor_delete(
    client,
    auth_headers_factory,
) -> None:
    created = client.post(
        "/api/v1/memories",
        json=_payload(),
        headers={"Idempotency-Key": "api-memory-create"},
    )
    assert created.status_code == 200
    assert created.json()["data"]["decision"] == "created"

    recalled = client.get(
        "/api/v1/memory-subjects/customer-memory-api/memories"
    )
    assert recalled.status_code == 200
    assert recalled.json()["data"][0]["value"] == "zh-cn"

    denied = client.request(
        "DELETE",
        "/api/v1/memory-subjects/customer-memory-api/memories/preference.language",
        json={"reason": "Customer requested deletion."},
        headers={"Idempotency-Key": "api-memory-delete-denied"},
    )
    assert denied.status_code == 403

    deleted = client.request(
        "DELETE",
        "/api/v1/memory-subjects/customer-memory-api/memories/preference.language",
        json={"reason": "Customer requested deletion."},
        headers={
            **auth_headers_factory(roles=frozenset({Role.SUPERVISOR})),
            "Idempotency-Key": "api-memory-delete",
        },
    )
    assert deleted.status_code == 200
    assert deleted.json()["data"]["decision"] == "deleted"
    assert client.get(
        "/api/v1/memory-subjects/customer-memory-api/memories"
    ).json()["data"] == []


def test_memory_api_records_non_writes_and_idempotency_conflicts(client) -> None:
    confirmation = client.post(
        "/api/v1/memories",
        json=_payload(source_type="model_inference"),
        headers={"Idempotency-Key": "api-memory-inference"},
    )
    rejected = client.post(
        "/api/v1/memories",
        json=_payload(
            memory_key="order.refund_eligible",
            value="true",
        ),
        headers={"Idempotency-Key": "api-memory-business-state"},
    )
    first = client.post(
        "/api/v1/memories",
        json=_payload(),
        headers={"Idempotency-Key": "api-memory-idempotent"},
    )
    replay = client.post(
        "/api/v1/memories",
        json=_payload(),
        headers={"Idempotency-Key": "api-memory-idempotent"},
    )
    conflict = client.post(
        "/api/v1/memories",
        json=_payload(value="en-US"),
        headers={"Idempotency-Key": "api-memory-idempotent"},
    )

    assert confirmation.json()["data"]["decision"] == "confirmation_required"
    assert rejected.json()["data"]["decision"] == "rejected"
    assert first.json()["data"] == replay.json()["data"]
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "memory_idempotency_conflict"


def test_memory_api_enforces_customer_subject_scope_and_value_contract(
    client,
    auth_headers_factory,
) -> None:
    customer_headers = auth_headers_factory(
        actor_id="customer-memory-api",
        roles=frozenset({Role.CUSTOMER}),
    )
    own = client.post(
        "/api/v1/memories",
        json=_payload(),
        headers={
            **customer_headers,
            "Idempotency-Key": "api-memory-customer-own",
        },
    )
    other = client.post(
        "/api/v1/memories",
        json=_payload(subject_id="customer-other"),
        headers={
            **customer_headers,
            "Idempotency-Key": "api-memory-customer-other",
        },
    )
    invalid_value = client.post(
        "/api/v1/memories",
        json=_payload(value="definitely-not-a-language-tag"),
        headers={"Idempotency-Key": "api-memory-invalid-value"},
    )

    assert own.status_code == 200
    assert other.status_code == 403
    assert invalid_value.status_code == 422
    assert invalid_value.json()["error"]["code"] == "memory_value_invalid"
