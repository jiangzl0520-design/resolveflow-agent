from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from app.db.database import Database
from app.domain.model_call import ModelCallRecord, ModelCallStatus
from app.repositories.sqlalchemy_model_call_repository import (
    SqlAlchemyModelCallRepository,
)
from tests.integration.database_helpers import migrated_sqlite_url


def test_model_call_record_round_trip_is_tenant_and_trace_scoped(
    tmp_path: Path,
) -> None:
    database = Database(migrated_sqlite_url(tmp_path))
    repository = SqlAlchemyModelCallRepository(database.session_factory)
    tenant_id = UUID("60000000-0000-0000-0000-000000000001")
    now = datetime.now(UTC)
    record = ModelCallRecord(
        id=uuid4(),
        tenant_id=tenant_id,
        operation="triage",
        provider="fake",
        model="fake-model",
        prompt_name="triage",
        prompt_version="1.0.0",
        prompt_hash="a" * 64,
        response_schema_name="InvestigationTriage",
        response_schema_hash="b" * 64,
        resource_type="ticket",
        resource_id="ticket-001",
        status=ModelCallStatus.SUCCEEDED,
        attempts=2,
        attempt_error_codes=("provider_timeout",),
        latency_ms=12.5,
        input_tokens=100,
        output_tokens=20,
        provider_request_id="provider-request",
        provider_response_id="provider-response",
        error_code=None,
        request_id="request-001",
        trace_id="trace-001",
        started_at=now,
        completed_at=now,
    )

    repository.add(record)

    assert repository.list_for_trace(tenant_id, "trace-001") == [record]
    assert repository.list_for_trace(
        UUID("60000000-0000-0000-0000-000000000002"),
        "trace-001",
    ) == []
    assert repository.list_for_trace(tenant_id, "other-trace") == []
    database.dispose()
