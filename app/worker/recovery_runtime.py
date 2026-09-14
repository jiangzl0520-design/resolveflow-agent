from dataclasses import dataclass

from app.core.config import Settings
from app.db.database import Database
from app.db.sqlalchemy_unit_of_work import (
    SqlAlchemyTicketUnitOfWorkFactory,
)
from app.repositories.authorization_audit_repository import (
    InMemoryAuthorizationAuditRepository,
)
from app.repositories.sqlalchemy_investigation_job_repository import (
    SqlAlchemyInvestigationJobLocator,
)
from app.services.authorization_service import AuthorizationService
from app.services.investigation_job_service import InvestigationJobService
from app.worker.celery_app import celery_app
from app.worker.celery_dispatcher import (
    CeleryInvestigationTaskDispatcher,
)


@dataclass(slots=True)
class RecoveryRuntime:
    database: Database
    job_service: InvestigationJobService

    def close(self) -> None:
        self.database.dispose()


def create_recovery_runtime(
    settings: Settings | None = None,
) -> RecoveryRuntime:
    resolved = settings or Settings.from_env()
    database = Database(resolved.database_url)
    unit_of_work_factory = SqlAlchemyTicketUnitOfWorkFactory(
        database.session_factory
    )
    locator = SqlAlchemyInvestigationJobLocator(database.session_factory)
    return RecoveryRuntime(
        database=database,
        job_service=InvestigationJobService(
            unit_of_work_factory,
            AuthorizationService(
                InMemoryAuthorizationAuditRepository()
            ),
            CeleryInvestigationTaskDispatcher(celery_app),
            locator,
            max_attempts=resolved.job_max_attempts,
        ),
    )
