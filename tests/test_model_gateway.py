from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.domain.investigation_triage import InvestigationTriage
from app.domain.model_call import ModelCallStatus
from app.llm.contracts import (
    RawProviderResponse,
    StructuredModelRequest,
)
from app.llm.errors import (
    ModelCallRecordingError,
    ModelOutputValidationError,
    ModelProviderAuthenticationError,
    ModelProviderRateLimitError,
    ModelProviderTimeoutError,
    ModelRequestRejectedError,
)
from app.llm.fake_provider import FakeLLMProvider
from app.llm.gateway import ModelGateway
from app.repositories.model_call_repository import (
    InMemoryModelCallRepository,
)

TENANT_ID = UUID("60000000-0000-0000-0000-000000000001")


class MutableTime:
    def __init__(self) -> None:
        self.wall = datetime(2026, 7, 24, 10, 0, tzinfo=UTC)
        self.monotonic = 100.0

    def wall_clock(self) -> datetime:
        current = self.wall
        self.wall += timedelta(milliseconds=10)
        return current

    def monotonic_clock(self) -> float:
        current = self.monotonic
        self.monotonic += 0.01
        return current


class FailingRecorder:
    def add(self, record) -> None:
        raise RuntimeError("database unavailable")

    def list_for_trace(self, tenant_id, trace_id):
        return []


def valid_output() -> dict[str, object]:
    return {
        "normalized_goal": "调查用户未收到包裹的问题",
        "required_evidence": [
            "order_status",
            "logistics_trace",
            "delivery_proof",
        ],
        "risk_flags": ["delivery_dispute"],
        "needs_human_attention": False,
        "decision_summary": "先核实订单、物流和签收证明。",
    }


def request() -> StructuredModelRequest[InvestigationTriage]:
    return StructuredModelRequest(
        tenant_id=TENANT_ID,
        operation="test_triage",
        prompt_name="test-prompt",
        prompt_version="1.0.0",
        prompt_hash="a" * 64,
        resource_type="ticket",
        resource_id="ticket-001",
        instructions="Return structured output.",
        input_text="Untrusted ticket text.",
        response_model=InvestigationTriage,
        request_id="request-001",
        trace_id="trace-001",
    )


def gateway(
    provider,
    recorder=None,
    *,
    sleeper=lambda _: None,
    mutable_time=None,
) -> ModelGateway:
    clock = mutable_time or MutableTime()
    return ModelGateway(
        provider,
        recorder or InMemoryModelCallRepository(),
        model="configured-model",
        reasoning_effort="low",
        max_output_tokens=500,
        max_attempts=3,
        retry_base_seconds=1,
        sleeper=sleeper,
        wall_clock=clock.wall_clock,
        monotonic_clock=clock.monotonic_clock,
    )


def test_gateway_validates_output_and_records_success() -> None:
    recorder = InMemoryModelCallRepository()
    provider = FakeLLMProvider(
        [
            RawProviderResponse(
                output=valid_output(),
                model="fake-model-v1",
                provider_request_id="provider-request",
                provider_response_id="provider-response",
                input_tokens=120,
                output_tokens=45,
            )
        ]
    )

    response = gateway(provider, recorder).generate(request())

    assert response.output.normalized_goal == "调查用户未收到包裹的问题"
    assert response.attempts == 1
    assert response.input_tokens == 120
    assert response.output_tokens == 45
    assert len(provider.requests) == 1
    record = recorder.records[0]
    assert record.id == response.call_id
    assert record.status is ModelCallStatus.SUCCEEDED
    assert record.prompt_version == "1.0.0"
    assert len(record.response_schema_hash) == 64
    assert record.resource_type == "ticket"
    assert record.resource_id == "ticket-001"
    assert record.attempt_error_codes == ()
    assert record.provider_request_id == "provider-request"


def test_gateway_retries_only_transient_provider_errors() -> None:
    recorder = InMemoryModelCallRepository()
    sleeps: list[float] = []
    provider = FakeLLMProvider(
        [
            ModelProviderTimeoutError(),
            ModelProviderRateLimitError(),
            RawProviderResponse(
                output=valid_output(),
                model="fake-model-v1",
            ),
        ]
    )

    response = gateway(
        provider,
        recorder,
        sleeper=sleeps.append,
    ).generate(request())

    assert response.attempts == 3
    assert sleeps == [1, 2]
    assert recorder.records[0].attempt_error_codes == (
        "provider_timeout",
        "provider_rate_limited",
    )


def test_invalid_structured_output_is_recorded_and_rejected() -> None:
    recorder = InMemoryModelCallRepository()
    invalid = {**valid_output(), "unexpected_internal_action": "refund"}

    with pytest.raises(ModelOutputValidationError):
        gateway(
            FakeLLMProvider(
                [
                    RawProviderResponse(
                        output=invalid,
                        model="fake-model-v1",
                    )
                ]
            ),
            recorder,
        ).generate(request())

    record = recorder.records[0]
    assert record.status is ModelCallStatus.FAILED
    assert record.error_code == "model_output_invalid"
    assert record.attempts == 1


def test_permanent_provider_error_is_not_retried() -> None:
    recorder = InMemoryModelCallRepository()
    provider = FakeLLMProvider(
        [ModelProviderAuthenticationError()]
    )

    with pytest.raises(ModelRequestRejectedError) as captured:
        gateway(provider, recorder).generate(request())

    assert captured.value.error_code == "provider_authentication_failed"
    assert len(provider.requests) == 1
    assert recorder.records[0].attempts == 1


def test_observability_failure_blocks_model_output() -> None:
    provider = FakeLLMProvider(
        [
            RawProviderResponse(
                output=valid_output(),
                model="fake-model-v1",
            )
        ]
    )

    with pytest.raises(ModelCallRecordingError):
        gateway(provider, FailingRecorder()).generate(request())
