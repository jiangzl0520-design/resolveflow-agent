from types import SimpleNamespace

import pytest

from app.knowledge.constants import KNOWLEDGE_EMBEDDING_DIMENSIONS
from app.knowledge.embeddings import OpenAIEmbeddingProvider
from app.knowledge.retrieval_errors import KnowledgeEmbeddingContractError


class RecordingEmbeddings:
    def __init__(self, vectors):
        self.vectors = vectors
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            data=[
                SimpleNamespace(index=index, embedding=vector)
                for index, vector in enumerate(self.vectors)
            ]
        )


class StubClient:
    def __init__(self, vectors):
        self.embeddings = RecordingEmbeddings(vectors)


def vector(position: int = 0) -> list[float]:
    values = [0.0] * KNOWLEDGE_EMBEDDING_DIMENSIONS
    values[position] = 1.0
    return values


def test_openai_embedding_provider_uses_fixed_contract() -> None:
    client = StubClient([vector(0), vector(1)])
    provider = OpenAIEmbeddingProvider(
        client,
        model="text-embedding-3-small",
        max_attempts=2,
        retry_base_seconds=0.01,
        sleeper=lambda _: None,
    )

    result = provider.embed(["first", "second"])

    assert result == (tuple(vector(0)), tuple(vector(1)))
    assert client.embeddings.calls == [
        {
            "model": "text-embedding-3-small",
            "input": ["first", "second"],
            "dimensions": KNOWLEDGE_EMBEDDING_DIMENSIONS,
            "encoding_format": "float",
        }
    ]


def test_openai_embedding_provider_rejects_wrong_dimensions() -> None:
    provider = OpenAIEmbeddingProvider(
        StubClient([[1.0, 0.0]]),
        model="text-embedding-3-small",
        max_attempts=1,
        retry_base_seconds=0.01,
    )

    with pytest.raises(KnowledgeEmbeddingContractError):
        provider.embed(["invalid"])

