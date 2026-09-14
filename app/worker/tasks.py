from uuid import UUID

from opentelemetry.trace import SpanKind

from app.core.config import Settings
from app.observability.tracing import (
    FailureDomain,
    add_safe_event,
    configure_global_tracing,
    extract_trace_context,
    mark_span_error,
    operation_span,
    set_safe_attributes,
)
from app.observability.metrics import get_metrics
from app.services.investigation_worker_service import WorkerOutcomeKind
from app.worker.celery_app import (
    PROCESS_INVESTIGATION_JOB_TASK,
    RECOVER_PENDING_INVESTIGATION_JOBS_TASK,
    celery_app,
)
from app.worker.recovery_runtime import create_recovery_runtime
from app.worker.runtime import create_worker_runtime


@celery_app.task(
    bind=True,
    name=PROCESS_INVESTIGATION_JOB_TASK,
    acks_late=True,
    reject_on_worker_lost=True,
    ignore_result=True,
    max_retries=None,
)
def process_investigation_job(self, job_id: str) -> None:
    settings = Settings.from_env()
    configure_global_tracing(settings)
    headers = getattr(self.request, "headers", None) or {}
    parent_context = extract_trace_context(headers)
    with operation_span(
        f"process {PROCESS_INVESTIGATION_JOB_TASK}",
        kind=SpanKind.CONSUMER,
        parent_context=parent_context,
        failure_domain=FailureDomain.WORKER,
        attributes={
            "messaging.system": "redis",
            "messaging.destination.name": "resolveflow.investigation",
            "messaging.operation.type": "process",
            "messaging.message.id": job_id,
            "resolveflow.component": "worker",
            "resolveflow.request_id": headers.get("request_id"),
            "resolveflow.trace_id": headers.get("trace_id"),
            "resolveflow.worker.retry_count": getattr(
                self.request,
                "retries",
                0,
            ),
        },
    ) as span:
        runtime = create_worker_runtime()
        try:
            worker_id = (
                getattr(self.request, "hostname", None)
                or "unknown-worker"
            )
            outcome = runtime.worker_service.run(
                UUID(job_id),
                worker_id=worker_id,
            )
        finally:
            runtime.close()

        set_safe_attributes(
            span,
            {
                "resolveflow.worker.outcome": outcome.kind.value,
                "resolveflow.worker.reason": outcome.reason,
                "resolveflow.worker.retry_after_seconds": (
                    outcome.retry_after_seconds
                ),
            },
        )
        get_metrics().record_worker(
            task="process_investigation",
            outcome=outcome.kind.value,
            reason=outcome.reason,
        )
        if outcome.kind is WorkerOutcomeKind.RETRY:
            mark_span_error(
                span,
                FailureDomain.WORKER,
                outcome.reason or "worker_retry",
                retryable=True,
            )
            add_safe_event(
                span,
                "worker.retry_scheduled",
                {"delay_seconds": outcome.retry_after_seconds or 1},
            )
            raise self.retry(
                countdown=outcome.retry_after_seconds or 1,
                max_retries=None,
            )
        if (
            outcome.kind in {WorkerOutcomeKind.TERMINAL, WorkerOutcomeKind.MISSING}
            and outcome.reason not in {"cancelled", "cancellation_pending"}
        ):
            mark_span_error(
                span,
                FailureDomain.WORKER,
                outcome.reason or "worker_terminal_failure",
                retryable=False,
            )


@celery_app.task(
    name=RECOVER_PENDING_INVESTIGATION_JOBS_TASK,
    ignore_result=True,
)
def recover_pending_investigation_jobs(limit: int = 100) -> None:
    settings = Settings.from_env()
    configure_global_tracing(settings)
    with operation_span(
        f"process {RECOVER_PENDING_INVESTIGATION_JOBS_TASK}",
        kind=SpanKind.CONSUMER,
        failure_domain=FailureDomain.WORKER,
        attributes={
            "messaging.system": "redis",
            "messaging.operation.type": "process",
            "resolveflow.component": "worker_recovery",
            "resolveflow.recovery.limit": limit,
        },
    ) as span:
        runtime = create_recovery_runtime()
        try:
            recovered = runtime.job_service.recover_pending_dispatches(
                limit=limit
            )
            get_metrics().record_worker(
                task="recover_pending",
                outcome="succeeded",
                reason="none",
            )
            set_safe_attributes(
                span,
                {"resolveflow.recovery.dispatched_count": recovered},
            )
        finally:
            runtime.close()
