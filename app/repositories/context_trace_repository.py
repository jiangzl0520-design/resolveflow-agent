from threading import RLock
from typing import Protocol

from app.domain.context import ContextBuildRun, ContextFragmentTrace


class ContextTraceRepository(Protocol):
    def record(
        self,
        run: ContextBuildRun,
        traces: tuple[ContextFragmentTrace, ...],
    ) -> None: ...


class InMemoryContextTraceRepository:
    def __init__(self) -> None:
        self._runs: list[ContextBuildRun] = []
        self._traces: list[ContextFragmentTrace] = []
        self._lock = RLock()

    def record(
        self,
        run: ContextBuildRun,
        traces: tuple[ContextFragmentTrace, ...],
    ) -> None:
        with self._lock:
            self._runs.append(run)
            self._traces.extend(traces)

    @property
    def runs(self) -> list[ContextBuildRun]:
        with self._lock:
            return list(self._runs)

    @property
    def traces(self) -> list[ContextFragmentTrace]:
        with self._lock:
            return list(self._traces)
