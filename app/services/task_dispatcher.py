from typing import Protocol

from app.domain.investigation_job import InvestigationJob


class InvestigationTaskDispatcher(Protocol):
    def dispatch(self, job: InvestigationJob) -> None: ...
