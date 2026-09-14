from dataclasses import dataclass

from app.core.config import Settings
from app.db.database import Database
from app.db.sqlalchemy_unit_of_work import (
    SqlAlchemyTicketUnitOfWorkFactory,
)
from app.repositories.sqlalchemy_investigation_job_repository import (
    SqlAlchemyInvestigationJobLocator,
)
from app.services.investigation_executor import (
    InvestigationBootstrapExecutor,
)
from app.services.investigation_worker_service import (
    InvestigationWorkerService,
)


@dataclass(slots=True)
class WorkerRuntime:
    database: Database
    worker_service: InvestigationWorkerService

    def close(self) -> None:
        self.database.dispose()


def create_worker_runtime(
    settings: Settings | None = None,
) -> WorkerRuntime:
    resolved = settings or Settings.from_env()
    database = Database(resolved.database_url)
    unit_of_work_factory = SqlAlchemyTicketUnitOfWorkFactory(
        database.session_factory
    )
    locator = SqlAlchemyInvestigationJobLocator(
        database.session_factory
    )
    return WorkerRuntime(
        database=database,
        worker_service=InvestigationWorkerService(
            unit_of_work_factory,
            locator,
            InvestigationBootstrapExecutor(),
            lease_seconds=resolved.job_lease_seconds,
            retry_base_seconds=resolved.job_retry_base_seconds,
            retry_max_seconds=resolved.job_retry_max_seconds,
        ),
    )
