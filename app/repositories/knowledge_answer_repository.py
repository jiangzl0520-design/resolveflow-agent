from threading import RLock
from typing import Protocol

from app.domain.grounded_answer import (
    KnowledgeAnswerCitationTrace,
    KnowledgeAnswerRun,
    KnowledgeAnswerStatus,
)


class KnowledgeAnswerRepository(Protocol):
    def add_run(self, run: KnowledgeAnswerRun) -> None: ...

    def finish_run(
        self,
        run: KnowledgeAnswerRun,
        citations: tuple[KnowledgeAnswerCitationTrace, ...],
    ) -> None: ...


class InMemoryKnowledgeAnswerRepository:
    def __init__(self) -> None:
        self._runs: dict[object, KnowledgeAnswerRun] = {}
        self._citations: list[KnowledgeAnswerCitationTrace] = []
        self._lock = RLock()

    def add_run(self, run: KnowledgeAnswerRun) -> None:
        with self._lock:
            if run.status is not KnowledgeAnswerStatus.PROCESSING:
                raise ValueError("A new answer run must be processing.")
            if run.id in self._runs:
                raise ValueError("Knowledge answer run already exists.")
            self._runs[run.id] = run

    def finish_run(
        self,
        run: KnowledgeAnswerRun,
        citations: tuple[KnowledgeAnswerCitationTrace, ...],
    ) -> None:
        with self._lock:
            current = self._runs.get(run.id)
            if (
                current is None
                or current.status is not KnowledgeAnswerStatus.PROCESSING
                or run.status is KnowledgeAnswerStatus.PROCESSING
            ):
                raise ValueError("Knowledge answer run cannot be finished.")
            self._runs[run.id] = run
            self._citations.extend(citations)

    @property
    def runs(self) -> list[KnowledgeAnswerRun]:
        with self._lock:
            return list(self._runs.values())

    @property
    def citations(self) -> list[KnowledgeAnswerCitationTrace]:
        with self._lock:
            return list(self._citations)
