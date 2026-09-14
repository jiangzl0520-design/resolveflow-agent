from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from app.repositories.idempotency_repository import IdempotencyRepository
from app.repositories.investigation_job_event_repository import (
    InvestigationJobEventRepository,
)
from app.repositories.investigation_job_repository import (
    InvestigationJobRepository,
)
from app.repositories.ticket_event_repository import TicketEventRepository
from app.repositories.ticket_repository import TicketRepository


class TicketUnitOfWork(Protocol):
    tickets: TicketRepository
    events: TicketEventRepository
    idempotency: IdempotencyRepository
    investigation_jobs: InvestigationJobRepository
    investigation_job_events: InvestigationJobEventRepository

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def commit(self) -> None: ...

    def flush(self) -> None: ...


class TicketUnitOfWorkFactory(Protocol):
    def __call__(self, tenant_id: UUID) -> TicketUnitOfWork: ...
