from celery import Celery

from app.core.config import Settings

INVESTIGATION_QUEUE = "resolveflow.investigation"
PROCESS_INVESTIGATION_JOB_TASK = (
    "resolveflow.process_investigation_job"
)
RECOVER_PENDING_INVESTIGATION_JOBS_TASK = (
    "resolveflow.recover_pending_investigation_jobs"
)


def create_celery_app(settings: Settings) -> Celery:
    application = Celery(
        "resolveflow",
        broker=settings.redis_url,
        include=["app.worker.tasks"],
    )
    visibility_timeout = settings.broker_visibility_timeout_seconds
    application.conf.update(
        task_default_queue=INVESTIGATION_QUEUE,
        task_serializer="json",
        accept_content=["json"],
        result_backend=None,
        task_ignore_result=True,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        broker_connection_retry_on_startup=True,
        broker_transport_options={
            "visibility_timeout": visibility_timeout,
        },
        visibility_timeout=visibility_timeout,
        task_soft_time_limit=settings.task_soft_time_limit_seconds,
        task_time_limit=settings.task_time_limit_seconds,
        worker_soft_shutdown_timeout=30.0,
        task_routes={
            PROCESS_INVESTIGATION_JOB_TASK: {
                "queue": INVESTIGATION_QUEUE,
            },
            RECOVER_PENDING_INVESTIGATION_JOBS_TASK: {
                "queue": INVESTIGATION_QUEUE,
            },
        },
        beat_schedule={
            "recover-pending-investigation-jobs": {
                "task": RECOVER_PENDING_INVESTIGATION_JOBS_TASK,
                "schedule": 30.0,
                "kwargs": {"limit": 100},
            },
        },
    )
    return application


celery_app = create_celery_app(Settings.from_env())
