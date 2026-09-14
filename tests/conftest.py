import pytest
from fastapi.testclient import TestClient
from uuid import UUID
from threading import RLock

from app.core.config import AppEnvironment, Settings
from app.core.errors import BrokerUnavailableError
from app.db.database import Database
from app.domain.auth import Role
from app.main import create_app
from app.domain.investigation_job import InvestigationJob
from tests.integration.database_helpers import migrated_sqlite_url

TEST_TENANT_ID = "10000000-0000-0000-0000-000000000001"
TEST_JWT_SECRET = "test-only-resolveflow-jwt-secret-at-least-32-characters"


class RecordingTaskDispatcher:
    def __init__(self) -> None:
        self._jobs: list[InvestigationJob] = []
        self._lock = RLock()
        self.failures_remaining = 0

    def dispatch(self, job: InvestigationJob) -> None:
        with self._lock:
            if self.failures_remaining > 0:
                self.failures_remaining -= 1
                raise BrokerUnavailableError("test broker failure")
            self._jobs.append(job)

    @property
    def jobs(self) -> list[InvestigationJob]:
        with self._lock:
            return list(self._jobs)


@pytest.fixture()
def task_dispatcher() -> RecordingTaskDispatcher:
    return RecordingTaskDispatcher()


@pytest.fixture()
def application(tmp_path, task_dispatcher):
    database_url = migrated_sqlite_url(tmp_path)
    database = Database(database_url)
    return create_app(
        database=database,
        task_dispatcher=task_dispatcher,
        settings=Settings(
            database_url=database_url,
            environment=AppEnvironment.TEST,
            jwt_secret=TEST_JWT_SECRET,
        ),
    )


@pytest.fixture()
def auth_headers_factory(application):
    def make_headers(
        *,
        actor_id: str = "agent-test-001",
        tenant_id: str = TEST_TENANT_ID,
        roles: frozenset[Role] = frozenset({Role.AGENT}),
    ) -> dict[str, str]:
        token = application.state.token_service.issue_access_token(
            actor_id=actor_id,
            tenant_id=UUID(tenant_id),
            roles=roles,
        )
        return {"Authorization": f"Bearer {token}"}

    return make_headers


@pytest.fixture()
def client(application, auth_headers_factory) -> TestClient:
    with TestClient(application) as test_client:
        test_client.headers.update(auth_headers_factory())
        yield test_client
