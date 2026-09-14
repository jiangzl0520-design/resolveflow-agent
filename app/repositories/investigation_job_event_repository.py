from typing import Protocol
from uuid import UUID

from app.domain.investigation_job import InvestigationJobEvent


class InvestigationJobEventRepository(Protocol):
    def add(
        self,
        event: InvestigationJobEvent,
    ) -> InvestigationJobEvent: ...

    def list_for_job(
        self,
        job_id: UUID,
    ) -> list[InvestigationJobEvent]: ...
