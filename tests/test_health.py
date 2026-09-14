from fastapi.testclient import TestClient


def test_health_check_returns_service_status_and_request_id(
    client: TestClient,
) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["data"] == {"status": "ok", "service": "resolveflow-api"}
    assert body["meta"]["request_id"] == response.headers["X-Request-ID"]

