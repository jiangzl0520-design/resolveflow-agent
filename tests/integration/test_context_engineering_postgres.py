from os import environ
from urllib.parse import urlparse
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.agent.models import AgentDecision
from app.context_engineering.builder import ContextBuilder
from app.context_engineering.tokens import TiktokenTokenCounter
from app.db.database import Database
from app.db.models import ContextBuildRunRecordModel, ModelCallRecordModel
from app.domain.context import (
    ContextBuildRequest,
    ContextFragmentCandidate,
    ContextSource,
    ContextTrustLevel,
    ContextWindowBudget,
)
from app.llm.contracts import RawProviderResponse, StructuredModelRequest
from app.llm.fake_provider import FakeLLMProvider
from app.llm.gateway import ModelGateway
from app.repositories.sqlalchemy_context_trace_repository import (
    SqlAlchemyContextTraceRepository,
)
from app.repositories.sqlalchemy_model_call_repository import (
    SqlAlchemyModelCallRepository,
)
from tests.integration.database_helpers import upgrade_database

TEST_DATABASE_URL = environ.get("TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        TEST_DATABASE_URL is None,
        reason="TEST_DATABASE_URL is not configured",
    ),
]


def test_context_build_and_model_call_link_on_real_postgres(
    request: pytest.FixtureRequest,
) -> None:
    assert TEST_DATABASE_URL is not None
    database_name = urlparse(TEST_DATABASE_URL).path.removeprefix("/")
    assert database_name.endswith("_test")
    upgrade_database(TEST_DATABASE_URL)
    database = Database(TEST_DATABASE_URL)
    request.addfinalizer(database.dispose)
    tenant_id = UUID("94000000-0000-0000-0000-000000000001")
    run_id = uuid4()
    result = ContextBuilder(
        TiktokenTokenCounter("gpt-5.6-terra"),
        SqlAlchemyContextTraceRepository(database.session_factory),
    ).build(
        ContextBuildRequest(
            tenant_id=tenant_id,
            agent_run_id=run_id,
            step_number=1,
            model="gpt-5.6-terra",
            budget=ContextWindowBudget(
                context_window_tokens=10_000,
                reserved_output_tokens=500,
                reserved_reasoning_tokens=500,
                safety_margin_tokens=500,
                max_input_tokens=2_000,
            ),
            candidates=(
                ContextFragmentCandidate(
                    fragment_id="system",
                    semantic_key="system",
                    source=ContextSource.SYSTEM_POLICY,
                    trust_level=ContextTrustLevel.SYSTEM,
                    content="Follow deterministic refund gates.",
                    priority=100,
                    relevance=100,
                    required=True,
                ),
                ContextFragmentCandidate(
                    fragment_id="goal",
                    semantic_key="goal",
                    source=ContextSource.CURRENT_GOAL,
                    trust_level=ContextTrustLevel.USER_PROVIDED,
                    content={"order_id": "10087"},
                    priority=100,
                    relevance=100,
                    required=True,
                ),
            ),
            response_schema=AgentDecision.model_json_schema(),
            request_id="day15-postgres-request",
            trace_id=f"day15-postgres-{run_id}",
        )
    )
    gateway = ModelGateway(
        FakeLLMProvider(
            [
                RawProviderResponse(
                    output={
                        "action": "escalate",
                        "reason": "Evidence is incomplete.",
                        "escalation_reason": "Manual evidence review required.",
                    },
                    model="fake-context-model",
                    input_tokens=result.run.actual_input_tokens,
                    output_tokens=15,
                )
            ]
        ),
        SqlAlchemyModelCallRepository(database.session_factory),
        model="fake-context-model",
        reasoning_effort="low",
        max_output_tokens=500,
        max_attempts=1,
        retry_base_seconds=0.001,
    )
    response = gateway.generate(
        StructuredModelRequest(
            tenant_id=tenant_id,
            operation="agent_decide_next_action",
            prompt_name="after-sales-agent-decision",
            prompt_version="1.2.0",
            prompt_hash="a" * 64,
            resource_type="agent_run",
            resource_id=str(run_id),
            instructions=result.instructions,
            input_text=result.input_text,
            response_model=AgentDecision,
            request_id="day15-postgres-request",
            trace_id=f"day15-postgres-{run_id}",
            context_build_id=result.run.id,
        )
    )

    with database.session_factory() as session:
        stored_build = session.get(
            ContextBuildRunRecordModel,
            result.run.id,
        )
        stored_call = session.scalar(
            select(ModelCallRecordModel).where(
                ModelCallRecordModel.id == response.call_id
            )
        )
    assert stored_build is not None
    assert stored_call is not None
    assert stored_call.context_build_id == stored_build.id
