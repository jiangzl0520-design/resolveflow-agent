from argparse import ArgumentParser
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import platform
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config

from app.db.database import Database
from app.db.sqlalchemy_unit_of_work import (
    SqlAlchemyTicketUnitOfWorkFactory,
)
from app.domain.auth import AuthenticatedActor, Role
from app.domain.investigation_job import InvestigationJobEventType
from app.domain.ticket import TicketCategory
from app.domain.ticket_event import TicketEventType
from app.repositories.authorization_audit_repository import (
    InMemoryAuthorizationAuditRepository,
)
from app.repositories.sqlalchemy_investigation_job_repository import (
    SqlAlchemyInvestigationJobLocator,
)
from app.services.authorization_service import AuthorizationService
from app.services.investigation_executor import (
    InvestigationBootstrapExecutor,
)
from app.services.investigation_job_service import (
    InvestigationJobService,
    SubmitInvestigationJobCommand,
)
from app.services.investigation_worker_service import (
    InvestigationWorkerService,
    WorkerOutcomeKind,
)
from app.services.ticket_service import CreateTicketCommand, TicketService

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = (
    ROOT
    / "evaluation"
    / "datasets"
    / "day05_duplicate_delivery_v1.json"
)
TENANT_ID = UUID("50000000-0000-0000-0000-000000000001")
ACTOR = AuthenticatedActor(
    actor_id="day05-evaluation-agent",
    tenant_id=TENANT_ID,
    roles=frozenset({Role.AGENT}),
)


class RecordingDispatcher:
    def __init__(self) -> None:
        self.job_ids: list[UUID] = []

    def dispatch(self, job) -> None:
        self.job_ids.append(job.id)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = min(
        len(ordered) - 1,
        max(0, int(round((len(ordered) - 1) * percentile))),
    )
    return ordered[index]


def _upgrade(database_url: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")


def run(dataset_path: Path) -> dict[str, object]:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    unique_jobs = int(dataset["unique_jobs"])
    deliveries_per_job = int(dataset["deliveries_per_job"])
    expired_lease_jobs = int(dataset["expired_lease_jobs"])
    if not 0 <= expired_lease_jobs <= unique_jobs:
        raise ValueError("expired_lease_jobs must be within unique_jobs.")

    baseline_effects = 0
    for _job_index in range(unique_jobs):
        for _delivery_index in range(deliveries_per_job):
            baseline_effects += 1

    with TemporaryDirectory(prefix="resolveflow-day05-") as temp_dir:
        database_path = Path(temp_dir) / "evaluation.sqlite3"
        database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
        _upgrade(database_url)
        database = Database(database_url)
        uow_factory = SqlAlchemyTicketUnitOfWorkFactory(
            database.session_factory
        )
        authorization = AuthorizationService(
            InMemoryAuthorizationAuditRepository()
        )
        ticket_service = TicketService(uow_factory, authorization)
        dispatcher = RecordingDispatcher()
        locator = SqlAlchemyInvestigationJobLocator(
            database.session_factory
        )
        job_service = InvestigationJobService(
            uow_factory,
            authorization,
            dispatcher,
            locator,
            max_attempts=3,
        )
        worker = InvestigationWorkerService(
            uow_factory,
            locator,
            InvestigationBootstrapExecutor(),
            lease_seconds=90,
            retry_base_seconds=2,
            retry_max_seconds=60,
        )

        job_ids: list[UUID] = []
        ticket_ids: list[UUID] = []
        for index in range(unique_jobs):
            ticket = ticket_service.create_ticket(
                ACTOR,
                CreateTicketCommand(
                    customer_id=f"synthetic-customer-{index:03d}",
                    subject=f"Synthetic investigation {index:03d}",
                    description=(
                        "Synthetic input for duplicate-delivery evaluation."
                    ),
                    category=TicketCategory.OTHER,
                    idempotency_key=f"day05-ticket-{index:03d}",
                ),
                request_id=f"day05-ticket-request-{index:03d}",
            ).ticket
            job = job_service.submit(
                ACTOR,
                SubmitInvestigationJobCommand(
                    ticket_id=ticket.id,
                    idempotency_key=f"day05-job-{index:03d}",
                ),
                request_id=f"day05-job-request-{index:03d}",
                trace_id=f"day05-trace-{index:03d}",
            ).job
            ticket_ids.append(ticket.id)
            job_ids.append(job.id)

        stale_now = datetime.now(UTC) - timedelta(seconds=91)
        for job_id in job_ids[:expired_lease_jobs]:
            with uow_factory(TENANT_ID) as unit_of_work:
                current = unit_of_work.investigation_jobs.get(job_id)
                if current is None:
                    raise RuntimeError(f"Missing job {job_id}.")
                claimed, _ = current.claim(
                    now=stale_now,
                    lease_token=uuid4(),
                    lease_duration=timedelta(seconds=90),
                )
                unit_of_work.investigation_jobs.update(
                    claimed,
                    expected_version=current.version,
                )
                unit_of_work.commit()

        latencies_ms: list[float] = []
        outcomes: list[WorkerOutcomeKind] = []
        for job_id in job_ids:
            for delivery_index in range(deliveries_per_job):
                started = perf_counter()
                outcome = worker.run(
                    job_id,
                    worker_id=f"evaluation-worker-{delivery_index}",
                )
                latencies_ms.append((perf_counter() - started) * 1000)
                outcomes.append(outcome.kind)

        side_effect_counts: list[int] = []
        recovered_jobs = 0
        with uow_factory(TENANT_ID) as unit_of_work:
            for ticket_id, job_id in zip(
                ticket_ids,
                job_ids,
                strict=True,
            ):
                side_effect_counts.append(
                    sum(
                        event.event_type is TicketEventType.STATUS_CHANGED
                        and event.payload.get("job_id") == str(job_id)
                        for event in unit_of_work.events.list_for_ticket(
                            ticket_id
                        )
                    )
                )
                if any(
                    event.event_type
                    is InvestigationJobEventType.LEASE_RECOVERED
                    for event in (
                        unit_of_work.investigation_job_events.list_for_job(
                            job_id
                        )
                    )
                ):
                    recovered_jobs += 1
        database.dispose()

    durable_effects = sum(side_effect_counts)
    total_deliveries = unique_jobs * deliveries_per_job
    report = {
        "report_id": "day05-reliability-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "dataset": dataset,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "database": "temporary SQLite; PostgreSQL concurrency is tested separately",
        },
        "baseline_without_idempotent_claim": {
            "deliveries": total_deliveries,
            "side_effects": baseline_effects,
            "duplicate_side_effects": baseline_effects - unique_jobs,
            "duplicate_side_effect_rate": round(
                (baseline_effects - unique_jobs) / total_deliveries,
                4,
            ),
        },
        "resolveflow_durable_job_engine": {
            "deliveries": total_deliveries,
            "successful_jobs": sum(
                outcome is WorkerOutcomeKind.SUCCEEDED
                for outcome in outcomes
            ),
            "side_effects": durable_effects,
            "duplicate_side_effects": sum(
                max(0, count - 1) for count in side_effect_counts
            ),
            "jobs_with_exactly_one_side_effect": sum(
                count == 1 for count in side_effect_counts
            ),
            "expired_lease_jobs_recovered": recovered_jobs,
            "p50_delivery_latency_ms": round(median(latencies_ms), 3),
            "p95_delivery_latency_ms": round(
                _percentile(latencies_ms, 0.95),
                3,
            ),
        },
        "interpretation_limits": [
            "This is a synthetic reliability experiment, not production business revenue.",
            "Latency is local-machine evidence and must not be generalized to production.",
            "The baseline intentionally represents an at-least-once consumer without an idempotent database claim.",
        ],
    }
    return report


def main() -> None:
    parser = ArgumentParser()
    parser.add_argument(
        "--dataset",
        type=Path,
        default=DEFAULT_DATASET,
    )
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()

    report = run(arguments.dataset)
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)


if __name__ == "__main__":
    main()
