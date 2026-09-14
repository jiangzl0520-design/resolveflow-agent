from celery import Celery
from kombu.exceptions import KombuError, OperationalError
from opentelemetry.trace import SpanKind

from app.core.errors import BrokerUnavailableError
from app.domain.investigation_job import InvestigationJob
from app.observability.tracing import (
    FailureDomain,
    inject_trace_context,
    mark_span_error,
    operation_span,
)
from app.worker.celery_app import (
    INVESTIGATION_QUEUE,
    PROCESS_INVESTIGATION_JOB_TASK,
)


class CeleryInvestigationTaskDispatcher:
    def __init__(self, celery_application: Celery) -> None:
        self._celery_application = celery_application

    def dispatch(self, job: InvestigationJob) -> None:
        with operation_span(
            f"send {INVESTIGATION_QUEUE}",
            kind=SpanKind.PRODUCER,
            failure_domain=FailureDomain.WORKER,
            attributes={
                "messaging.system": "redis",
                "messaging.destination.name": INVESTIGATION_QUEUE,
                "messaging.operation.type": "send",
                "messaging.message.id": str(job.id),
                "resolveflow.component": "worker_dispatch",
                "resolveflow.request_id": job.request_id,
                "resolveflow.trace_id": job.trace_id,
            },
        ) as span:
            headers = {
                "request_id": job.request_id,
                "trace_id": job.trace_id,
            }
            inject_trace_context(headers)
            try:
                self._celery_application.send_task(
                    PROCESS_INVESTIGATION_JOB_TASK,
                    args=[str(job.id)],
                    task_id=str(job.id),
                    queue=INVESTIGATION_QUEUE,
                    headers=headers,
                    retry=False,
                )
            except (KombuError, OperationalError, OSError) as exc:
                mark_span_error(
                    span,
                    FailureDomain.WORKER,
                    "broker_unavailable",
                    retryable=True,
                )
                raise BrokerUnavailableError(
                    "Investigation broker is unavailable."
                ) from exc
