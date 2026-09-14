from dataclasses import dataclass
from typing import Protocol

from app.domain.investigation_job import InvestigationJob


class RetryableInvestigationError(Exception):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


class PermanentInvestigationError(Exception):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


@dataclass(frozen=True, slots=True)
class InvestigationPreparation:
    summary: str


class InvestigationExecutor(Protocol):
    def prepare(
        self,
        job: InvestigationJob,
    ) -> InvestigationPreparation: ...


class InvestigationBootstrapExecutor:
    """Day 5 deterministic placeholder for the Agent Runtime added later."""

    def prepare(
        self,
        job: InvestigationJob,
    ) -> InvestigationPreparation:
        return InvestigationPreparation(
            summary="Investigation worker accepted the durable job."
        )
