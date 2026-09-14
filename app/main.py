from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
import logging
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Request
from opentelemetry.trace import SpanKind

from app.api.exception_handlers import register_exception_handlers
from app.api.routes import auth, health, investigation_jobs, memories, metrics, tickets
from app.core.config import AppEnvironment, Settings
from app.core.security import JwtTokenService
from app.db.database import Database
from app.db.sqlalchemy_unit_of_work import SqlAlchemyTicketUnitOfWorkFactory
from app.observability.tracing import (
    FailureDomain,
    configure_global_tracing,
    current_trace_id,
    extract_trace_context,
    mark_span_error,
    operation_span,
    set_safe_attributes,
)
from app.observability.logging import configure_logging, log_context, log_event
from app.observability.metrics import get_metrics
from app.repositories.authorization_audit_repository import (
    InMemoryAuthorizationAuditRepository,
)
from app.repositories.sqlalchemy_authorization_audit_repository import (
    SqlAlchemyAuthorizationAuditRepository,
)
from app.repositories.investigation_job_repository import (
    InvestigationJobLocator,
    NullInvestigationJobLocator,
)
from app.repositories.sqlalchemy_investigation_job_repository import (
    SqlAlchemyInvestigationJobLocator,
)
from app.repositories.memory_repository import InMemoryMemoryRepository
from app.repositories.sqlalchemy_memory_repository import (
    SqlAlchemyMemoryRepository,
)
from app.services.authorization_service import AuthorizationService
from app.services.investigation_job_service import InvestigationJobService
from app.services.memory_service import MemoryService
from app.services.task_dispatcher import InvestigationTaskDispatcher
from app.services.ticket_service import TicketService
from app.services.unit_of_work import TicketUnitOfWorkFactory
from app.worker.celery_app import create_celery_app
from app.worker.celery_dispatcher import (
    CeleryInvestigationTaskDispatcher,
)


def create_app(
    *,
    unit_of_work_factory: TicketUnitOfWorkFactory | None = None,
    authorization_service: AuthorizationService | None = None,
    task_dispatcher: InvestigationTaskDispatcher | None = None,
    job_locator: InvestigationJobLocator | None = None,
    database: Database | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    """Create an application with production or explicitly supplied adapters."""
    resolved_settings = settings or Settings.from_env()
    configure_global_tracing(resolved_settings)
    configure_logging(resolved_settings.log_level)
    owned_database: Database | None = None
    if unit_of_work_factory is None:
        owned_database = database or Database(
            resolved_settings.database_url
        )
        unit_of_work_factory = SqlAlchemyTicketUnitOfWorkFactory(
            owned_database.session_factory
        )
    if authorization_service is None:
        if owned_database is not None:
            audit_repository = SqlAlchemyAuthorizationAuditRepository(
                owned_database.session_factory
            )
        else:
            audit_repository = InMemoryAuthorizationAuditRepository()
        authorization_service = AuthorizationService(audit_repository)
    if job_locator is None:
        if owned_database is not None:
            job_locator = SqlAlchemyInvestigationJobLocator(
                owned_database.session_factory
            )
        else:
            job_locator = NullInvestigationJobLocator()
    if task_dispatcher is None:
        task_dispatcher = CeleryInvestigationTaskDispatcher(
            create_celery_app(resolved_settings)
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if owned_database is not None:
            owned_database.dispose()

    application = FastAPI(
        title="ResolveFlow API",
        version="0.1.0",
        description="Backend API for investigating and resolving support tickets.",
        lifespan=lifespan,
    )

    application.state.authorization_service = authorization_service
    memory_repository = (
        SqlAlchemyMemoryRepository(owned_database.session_factory)
        if owned_database is not None
        else InMemoryMemoryRepository()
    )
    application.state.memory_service = MemoryService(
        memory_repository,
        authorization_service,
    )
    application.state.ticket_service = TicketService(
        unit_of_work_factory,
        authorization_service,
    )
    application.state.investigation_job_service = (
        InvestigationJobService(
            unit_of_work_factory,
            authorization_service,
            task_dispatcher,
            job_locator,
            max_attempts=resolved_settings.job_max_attempts,
        )
    )
    application.state.job_locator = job_locator
    application.state.task_dispatcher = task_dispatcher
    application.state.token_service = JwtTokenService(resolved_settings)
    application.state.database = owned_database

    @application.middleware("http")
    async def attach_request_id(request: Request, call_next):
        request_id = str(uuid4())
        started = perf_counter()
        request_metrics = get_metrics()
        request_metrics.http_in_progress.labels(request.method.upper()).inc()
        parent_context = extract_trace_context(request.headers)
        with operation_span(
            f"HTTP {request.method}",
            kind=SpanKind.SERVER,
            parent_context=parent_context,
            failure_domain=FailureDomain.API,
            attributes={
                "http.request.method": request.method,
                "url.scheme": request.url.scheme,
                "resolveflow.component": "api",
                "resolveflow.request_id": request_id,
            },
        ) as span:
            trace_id = current_trace_id(fallback=str(uuid4()))
            request.state.request_id = request_id
            request.state.trace_id = trace_id
            with log_context(request_id=request_id, trace_id=trace_id):
                try:
                    response = await call_next(request)
                except BaseException as exc:
                    duration = perf_counter() - started
                    route = request.scope.get("route")
                    route_path = getattr(route, "path", None) or "__unmatched__"
                    request_metrics.observe_http(
                        method=request.method,
                        route=route_path,
                        status_code=500,
                        duration=duration,
                    )
                    request_metrics.http_in_progress.labels(
                        request.method.upper()
                    ).dec()
                    log_event(
                        logging.getLogger("resolveflow.http"),
                        "http.request.failed",
                        level=logging.ERROR,
                        method=request.method,
                        route=route_path,
                        status_code=500,
                        duration_ms=round(duration * 1000, 3),
                        error_type=type(exc).__name__,
                    )
                    raise
            route = request.scope.get("route")
            route_path = getattr(route, "path", None) or "__unmatched__"
            if route_path and hasattr(span, "set_name"):
                span.set_name(f"{request.method} {route_path}")
            set_safe_attributes(
                span,
                {
                    "http.route": route_path,
                    "http.response.status_code": response.status_code,
                    "resolveflow.trace_id": trace_id,
                },
            )
            if response.status_code >= 500:
                mark_span_error(
                    span,
                    FailureDomain.API,
                    f"http_{response.status_code}",
                )
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Trace-ID"] = trace_id
            duration = perf_counter() - started
            request_metrics.observe_http(
                method=request.method,
                route=route_path,
                status_code=response.status_code,
                duration=duration,
            )
            request_metrics.http_in_progress.labels(
                request.method.upper()
            ).dec()
            with log_context(request_id=request_id, trace_id=trace_id):
                log_event(
                    logging.getLogger("resolveflow.http"),
                    "http.request.completed",
                    method=request.method,
                    route=route_path,
                    status_code=response.status_code,
                    duration_ms=round(duration * 1000, 3),
                )
            return response

    register_exception_handlers(application)
    application.include_router(health.router)
    application.include_router(metrics.router)
    if resolved_settings.environment is AppEnvironment.DEVELOPMENT:
        application.include_router(auth.development_router, prefix="/api/v1")
    application.include_router(auth.authorization_router, prefix="/api/v1")
    application.include_router(tickets.router, prefix="/api/v1")
    application.include_router(memories.router, prefix="/api/v1")
    application.include_router(
        investigation_jobs.router,
        prefix="/api/v1",
    )
    return application


app = create_app()
