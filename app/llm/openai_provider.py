from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    ContentFilterFinishReasonError,
    InternalServerError,
    LengthFinishReasonError,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
)

from app.llm.contracts import (
    ProviderModelRequest,
    RawProviderResponse,
    StructuredLLMProvider,
    StructuredOutput,
)
from app.llm.errors import (
    ModelProviderAuthenticationError,
    ModelProviderConnectionError,
    ModelProviderInvalidRequestError,
    ModelProviderOutputTruncatedError,
    ModelProviderRateLimitError,
    ModelProviderRefusalError,
    ModelProviderServerError,
    ModelProviderTimeoutError,
)


class OpenAIResponsesProvider(StructuredLLMProvider):
    def __init__(self, client: Any) -> None:
        self._client = client

    @property
    def name(self) -> str:
        return "openai"

    def complete(
        self,
        request: ProviderModelRequest[StructuredOutput],
    ) -> RawProviderResponse:
        try:
            response = self._client.responses.parse(
                model=request.model,
                instructions=request.instructions,
                input=request.input_text,
                text_format=request.response_model,
                reasoning={"effort": request.reasoning_effort},
                max_output_tokens=request.max_output_tokens,
                metadata={
                    "trace_id": request.trace_id,
                    "prompt_name": request.prompt_name,
                    "prompt_version": request.prompt_version,
                },
                store=False,
            )
        except APITimeoutError as exc:
            raise ModelProviderTimeoutError() from exc
        except ContentFilterFinishReasonError as exc:
            raise ModelProviderRefusalError() from exc
        except LengthFinishReasonError as exc:
            raise ModelProviderOutputTruncatedError() from exc
        except RateLimitError as exc:
            raise ModelProviderRateLimitError() from exc
        except (AuthenticationError, PermissionDeniedError) as exc:
            raise ModelProviderAuthenticationError() from exc
        except (
            BadRequestError,
            UnprocessableEntityError,
        ) as exc:
            raise ModelProviderInvalidRequestError() from exc
        except InternalServerError as exc:
            raise ModelProviderServerError() from exc
        except APIConnectionError as exc:
            raise ModelProviderConnectionError() from exc
        except APIStatusError as exc:
            if exc.status_code >= 500:
                raise ModelProviderServerError() from exc
            raise ModelProviderInvalidRequestError() from exc

        parsed = response.output_parsed
        if parsed is None:
            raise ModelProviderRefusalError()
        usage = response.usage
        return RawProviderResponse(
            output=parsed,
            model=response.model,
            provider_request_id=getattr(
                response,
                "_request_id",
                None,
            ),
            provider_response_id=response.id,
            input_tokens=(
                usage.input_tokens if usage is not None else None
            ),
            output_tokens=(
                usage.output_tokens if usage is not None else None
            ),
        )
