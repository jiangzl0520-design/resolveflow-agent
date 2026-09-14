from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.domain.investigation_job import (
    InvestigationJob,
    InvestigationJobType,
)


class InvestigationJobRepository(Protocol):
    def add(self, job: InvestigationJob) -> InvestigationJob: ...

    def get(self, job_id: UUID) -> InvestigationJob | None: ...

    def get_by_idempotency(
        self,
        job_type: InvestigationJobType,
        idempotency_key: str,
    ) -> InvestigationJob | None: ...

    def update(
        self,
        job: InvestigationJob,
        *,
        expected_version: int,
    ) -> InvestigationJob: ...

    def record_dispatch_result(
        self,
        job_id: UUID,
        *,
        dispatched_at: datetime | None,
        error_code: str | None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class JobLocation:
    job_id: UUID
    tenant_id: UUID


class InvestigationJobLocator(Protocol):
    def locate(self, job_id: UUID) -> JobLocation | None: ...

    def list_pending_dispatch(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> list[JobLocation]: ...


class NullInvestigationJobLocator:
    def locate(self, job_id: UUID) -> JobLocation | None:
        return None

    def list_pending_dispatch(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> list[JobLocation]:
        return []
