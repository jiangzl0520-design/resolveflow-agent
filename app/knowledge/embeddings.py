from collections.abc import Callable, Sequence
from math import isfinite
from time import sleep
from typing import Any, Protocol

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    OpenAI,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
)

from app.core.config import Settings
from app.knowledge.constants import KNOWLEDGE_EMBEDDING_DIMENSIONS
from app.knowledge.retrieval_errors import (
    KnowledgeEmbeddingConfigurationError,
    KnowledgeEmbeddingContractError,
    KnowledgeEmbeddingProviderError,
)


class EmbeddingProvider(Protocol):
    @property
    def model(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]: ...


class OpenAIEmbeddingProvider:
    def __init__(
        self,
        client: Any,
        *,
        model: str,
        max_attempts: int,
        retry_base_seconds: float,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        self._client = client
        self._model = model
        self._max_attempts = max_attempts
        self._retry_base_seconds = retry_base_seconds
        self._sleeper = sleeper

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return KNOWLEDGE_EMBEDDING_DIMENSIONS

    def embed(
        self,
        texts: Sequence[str],
    ) -> tuple[tuple[float, ...], ...]:
        if not texts:
            return ()
        response = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._client.embeddings.create(
                    model=self._model,
                    input=list(texts),
                    dimensions=self.dimensions,
                    encoding_format="float",
                )
                break
            except (APITimeoutError, APIConnectionError, RateLimitError) as exc:
                if attempt < self._max_attempts:
                    self._sleeper(
                        self._retry_base_seconds * (2 ** (attempt - 1))
                    )
                    continue
                raise KnowledgeEmbeddingProviderError(
                    "knowledge_embedding_provider_unavailable",
                    "Knowledge embedding provider is temporarily unavailable.",
                    retryable=True,
                ) from exc
            except InternalServerError as exc:
                if attempt < self._max_attempts:
                    self._sleeper(
                        self._retry_base_seconds * (2 ** (attempt - 1))
                    )
                    continue
                raise KnowledgeEmbeddingProviderError(
                    "knowledge_embedding_provider_server_error",
                    "Knowledge embedding provider returned a server error.",
                    retryable=True,
                ) from exc
            except (AuthenticationError, PermissionDeniedError) as exc:
                raise KnowledgeEmbeddingProviderError(
                    "knowledge_embedding_provider_authentication_error",
                    "Knowledge embedding provider rejected authentication.",
                    retryable=False,
                ) from exc
            except (BadRequestError, UnprocessableEntityError) as exc:
                raise KnowledgeEmbeddingProviderError(
                    "knowledge_embedding_provider_invalid_request",
                    "Knowledge embedding request was rejected.",
                    retryable=False,
                ) from exc
            except APIStatusError as exc:
                raise KnowledgeEmbeddingProviderError(
                    "knowledge_embedding_provider_status_error",
                    "Knowledge embedding provider returned an unexpected status.",
                    retryable=exc.status_code >= 500,
                ) from exc
        if response is None:
            raise AssertionError("Embedding retry loop exited unexpectedly.")
        ordered = sorted(response.data, key=lambda item: item.index)
        vectors = tuple(tuple(float(value) for value in item.embedding) for item in ordered)
        _validate_vectors(vectors, expected_count=len(texts), dimensions=self.dimensions)
        return vectors


class OpenAIEmbeddingRuntime:
    def __init__(self, provider: OpenAIEmbeddingProvider, client: OpenAI):
        self.provider = provider
        self.client = client

    def close(self) -> None:
        self.client.close()


def create_openai_embedding_runtime(
    settings: Settings,
) -> OpenAIEmbeddingRuntime:
    if not settings.openai_api_key:
        raise KnowledgeEmbeddingConfigurationError()
    client = OpenAI(
        api_key=settings.openai_api_key,
        timeout=settings.knowledge_embedding_timeout_seconds,
        max_retries=0,
    )
    return OpenAIEmbeddingRuntime(
        OpenAIEmbeddingProvider(
            client,
            model=settings.knowledge_embedding_model,
            max_attempts=settings.knowledge_embedding_max_attempts,
            retry_base_seconds=(
                settings.knowledge_embedding_retry_base_seconds
            ),
        ),
        client,
    )


def _validate_vectors(
    vectors: Sequence[Sequence[float]],
    *,
    expected_count: int,
    dimensions: int,
) -> None:
    if len(vectors) != expected_count:
        raise KnowledgeEmbeddingContractError()
    if any(len(vector) != dimensions for vector in vectors):
        raise KnowledgeEmbeddingContractError()
    if any(not isfinite(value) for vector in vectors for value in vector):
        raise KnowledgeEmbeddingContractError()

