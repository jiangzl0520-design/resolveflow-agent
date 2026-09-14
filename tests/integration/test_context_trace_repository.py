from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select

from app.db.database import Database
from app.db.models import (
    ContextBuildRunRecordModel,
    ContextFragmentTraceRecordModel,
    ModelCallRecordModel,
)
from app.domain.context import (
    ContextBuildRun,
    ContextBuildStatus,
    ContextDecisionReason,
    ContextFragmentTrace,
    ContextSource,
    ContextTrustLevel,
)
from app.domain.model_call import ModelCallRecord, ModelCallStatus
from app.repositories.sqlalchemy_context_trace_repository import (
    SqlAlchemyContextTraceRepository,
)
from app.repositories.sqlalchemy_model_call_repository import (
    SqlAlchemyModelCallRepository,
)
from tests.integration.database_helpers import migrated_sqlite_url


def test_context_trace_and_model_call_are_linked_without_raw_content(
    tmp_path: Path,
) -> None:
    database = Database(migrated_sqlite_url(tmp_path))
    tenant_id = UUID("92000000-0000-0000-0000-000000000001")
    build_id = uuid4()
    now = datetime.now(UTC)
    run = ContextBuildRun(
        id=build_id,
        tenant_id=tenant_id,
        agent_run_id=uuid4(),
        step_number=1,
        model="test-model",
        status=ContextBuildStatus.SUCCEEDED,
        context_window_tokens=10_000,
        reserved_output_tokens=500,
        reserved_reasoning_tokens=500,
        safety_margin_tokens=500,
        input_budget_tokens=2_000,
        actual_input_tokens=300,
        included_fragment_count=1,
        dropped_fragment_count=1,
        error_code=None,
        request_id="request-context",
        trace_id="trace-context",
        created_at=now,
    )
    trace = ContextFragmentTrace(
        build_run_id=build_id,
        fragment_id="goal",
        semantic_key="goal",
        source=ContextSource.CURRENT_GOAL,
        trust_level=ContextTrustLevel.USER_PROVIDED,
        content_hash="a" * 64,
        priority=100,
        relevance=100,
        ordinal=0,
        estimated_tokens=20,
        included=True,
        decision_reason=ContextDecisionReason.REQUIRED,
    )
    SqlAlchemyContextTraceRepository(database.session_factory).record(
        run,
        (trace,),
    )
    model_record = ModelCallRecord(
        id=uuid4(),
        tenant_id=tenant_id,
        operation="agent_decide_next_action",
        provider="fake",
        model="test-model",
        prompt_name="agent",
        prompt_version="1.2.0",
        prompt_hash="b" * 64,
        response_schema_name="AgentDecision",
        response_schema_hash="c" * 64,
        resource_type="agent_run",
        resource_id=str(run.agent_run_id),
        status=ModelCallStatus.SUCCEEDED,
        attempts=1,
        attempt_error_codes=(),
        latency_ms=10,
        input_tokens=300,
        output_tokens=20,
        provider_request_id=None,
        provider_response_id=None,
        error_code=None,
        request_id=run.request_id,
        trace_id=run.trace_id,
        started_at=now,
        completed_at=now,
        context_build_id=build_id,
    )
    SqlAlchemyModelCallRepository(database.session_factory).add(model_record)

    with database.session_factory() as session:
        stored_run = session.get(ContextBuildRunRecordModel, build_id)
        stored_trace = session.scalar(
            select(ContextFragmentTraceRecordModel).where(
                ContextFragmentTraceRecordModel.build_run_id == build_id
            )
        )
        stored_call = session.get(ModelCallRecordModel, model_record.id)
    assert stored_run is not None
    assert stored_trace is not None
    assert stored_trace.content_hash == "a" * 64
    assert not hasattr(stored_trace, "content")
    assert stored_call is not None
    assert stored_call.context_build_id == build_id
    database.dispose()
