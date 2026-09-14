from collections.abc import Iterable
from threading import RLock

from app.llm.contracts import (
    ProviderModelRequest,
    RawProviderResponse,
    StructuredLLMProvider,
    StructuredOutput,
)


class FakeLLMProvider(StructuredLLMProvider):
    def __init__(
        self,
        outcomes: Iterable[RawProviderResponse | Exception],
    ) -> None:
        self._outcomes = list(outcomes)
        self._requests: list[ProviderModelRequest] = []
        self._lock = RLock()

    @property
    def name(self) -> str:
        return "fake"

    @property
    def requests(self) -> list[ProviderModelRequest]:
        with self._lock:
            return list(self._requests)

    def complete(
        self,
        request: ProviderModelRequest[StructuredOutput],
    ) -> RawProviderResponse:
        with self._lock:
            self._requests.append(request)
            if not self._outcomes:
                raise AssertionError("Fake LLM has no scripted outcome.")
            outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
