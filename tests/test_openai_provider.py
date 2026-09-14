from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError, RateLimitError

from app.domain.investigation_triage import InvestigationTriage
from app.llm.contracts import ProviderModelRequest
from app.llm.errors import (
    ModelProviderRateLimitError,
    ModelProviderRefusalError,
    ModelProviderTimeoutError,
)
from app.llm.openai_provider import OpenAIResponsesProvider
from tests.test_model_gateway import valid_output


class Responses:
    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, object]] = []

    def parse(self, **options):
        self.calls.append(options)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class Client:
    def __init__(self, outcome) -> None:
        self.responses = Responses(outcome)


def _request() -> ProviderModelRequest[InvestigationTriage]:
    return ProviderModelRequest(
        model="gpt-test",
        reasoning_effort="low",
        max_output_tokens=500,
        instructions="system instructions",
        input_text="untrusted ticket",
        response_model=InvestigationTriage,
        prompt_name="triage",
        prompt_version="1.0.0",
        request_id="request-001",
        trace_id="trace-001",
    )


def test_openai_adapter_uses_responses_parse_and_maps_usage() -> None:
    parsed = InvestigationTriage.model_validate(valid_output())
    response = SimpleNamespace(
        output_parsed=parsed,
        model="gpt-test-snapshot",
        id="response-001",
        _request_id="openai-request-001",
        usage=SimpleNamespace(input_tokens=111, output_tokens=22),
    )
    client = Client(response)

    result = OpenAIResponsesProvider(client).complete(_request())

    call = client.responses.calls[0]
    assert call["model"] == "gpt-test"
    assert call["text_format"] is InvestigationTriage
    assert call["reasoning"] == {"effort": "low"}
    assert call["store"] is False
    assert call["metadata"]["trace_id"] == "trace-001"
    assert result.output == parsed
    assert result.model == "gpt-test-snapshot"
    assert result.provider_request_id == "openai-request-001"
    assert result.input_tokens == 111
    assert result.output_tokens == 22


def test_openai_adapter_maps_timeout_and_rate_limit() -> None:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    timeout_client = Client(APITimeoutError(request))

    with pytest.raises(ModelProviderTimeoutError):
        OpenAIResponsesProvider(timeout_client).complete(_request())

    response = httpx.Response(429, request=request)
    rate_limit_client = Client(
        RateLimitError("rate limited", response=response, body=None)
    )
    with pytest.raises(ModelProviderRateLimitError):
        OpenAIResponsesProvider(rate_limit_client).complete(_request())


def test_openai_adapter_rejects_missing_parsed_output() -> None:
    response = SimpleNamespace(
        output_parsed=None,
        model="gpt-test",
        id="response-refused",
        _request_id="request-refused",
        usage=None,
    )

    with pytest.raises(ModelProviderRefusalError):
        OpenAIResponsesProvider(Client(response)).complete(_request())
