from types import TracebackType
from typing import Self
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import StorageUnavailableError
from app.repositories.sqlalchemy_idempotency_repository import (
    SqlAlchemyIdempotencyRepository,
)
from app.repositories.sqlalchemy_investigation_job_event_repository import (
    SqlAlchemyInvestigationJobEventRepository,
)
from app.repositories.sqlalchemy_investigation_job_repository import (
    SqlAlchemyInvestigationJobRepository,
)
from app.repositories.sqlalchemy_ticket_event_repository import (
    SqlAlchemyTicketEventRepository,
)
from app.repositories.sqlalchemy_ticket_repository import (
    SqlAlchemyTicketRepository,
)


class SqlAlchemyTicketUnitOfWork:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        tenant_id: UUID,
    ) -> None:
        self._session_factory = session_factory
        self._tenant_id = tenant_id

    def __enter__(self) -> Self:
        self._session = self._session_factory()
        self.tickets = SqlAlchemyTicketRepository(
            self._session,
            self._tenant_id,
        )
        self.events = SqlAlchemyTicketEventRepository(
            self._session,
            self._tenant_id,
        )
        self.idempotency = SqlAlchemyIdempotencyRepository(
            self._session,
            self._tenant_id,
        )
        self.investigation_jobs = SqlAlchemyInvestigationJobRepository(
            self._session,
            self._tenant_id,
        )
        self.investigation_job_events = (
            SqlAlchemyInvestigationJobEventRepository(
                self._session,
                self._tenant_id,
            )
        )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            self._session.rollback()
        self._session.close()

    def commit(self) -> None:
        try:
            self._session.commit()
        except SQLAlchemyError as exc:
            self._session.rollback()
            raise StorageUnavailableError(
                "Ticket transaction failed."
            ) from exc

    def flush(self) -> None:
        try:
            self._session.flush()
        except SQLAlchemyError as exc:
            self._session.rollback()
            raise StorageUnavailableError(
                "Ticket transaction flush failed."
            ) from exc


class SqlAlchemyTicketUnitOfWorkFactory:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def __call__(self, tenant_id: UUID) -> SqlAlchemyTicketUnitOfWork:
        return SqlAlchemyTicketUnitOfWork(
            self._session_factory,
            tenant_id,
        )
