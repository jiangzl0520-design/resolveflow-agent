from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.errors import (
    AuthenticationError,
    AuthorizationDeniedError,
    ConcurrentTicketUpdateError,
    ConcurrentJobUpdateError,
    IdempotencyConflictError,
    StorageUnavailableError,
    InvestigationJobNotFoundError,
    TicketNotFoundError,
)
from app.domain.ticket import InvalidTicketTransitionError
from app.schemas.common import ErrorDetail, ErrorResponse, ResponseMeta
from app.memory.errors import (
    MemoryIdempotencyConflictError,
    MemoryPolicyValidationError,
    MemoryReadError,
    MemoryStorageError,
)


def register_exception_handlers(application: FastAPI) -> None:
    @application.exception_handler(AuthenticationError)
    async def handle_authentication_error(
        request: Request, exc: AuthenticationError
    ) -> JSONResponse:
        body = ErrorResponse(
            error=ErrorDetail(
                code="authentication_required",
                message="A valid bearer access token is required.",
            ),
            meta=ResponseMeta(request_id=request.state.request_id),
        )
        return JSONResponse(
            status_code=401,
            content=body.model_dump(mode="json"),
            headers={"WWW-Authenticate": "Bearer"},
        )

    @application.exception_handler(AuthorizationDeniedError)
    async def handle_authorization_denied(
        request: Request, exc: AuthorizationDeniedError
    ) -> JSONResponse:
        body = ErrorResponse(
            error=ErrorDetail(
                code="permission_denied",
                message="The authenticated identity cannot perform this operation.",
            ),
            meta=ResponseMeta(request_id=request.state.request_id),
        )
        return JSONResponse(
            status_code=403,
            content=body.model_dump(mode="json"),
        )

    @application.exception_handler(StorageUnavailableError)
    async def handle_storage_unavailable(
        request: Request, exc: StorageUnavailableError
    ) -> JSONResponse:
        body = ErrorResponse(
            error=ErrorDetail(
                code="storage_unavailable",
                message="Persistent storage is temporarily unavailable.",
            ),
            meta=ResponseMeta(request_id=request.state.request_id),
        )
        return JSONResponse(status_code=503, content=body.model_dump(mode="json"))

    @application.exception_handler(TicketNotFoundError)
    async def handle_ticket_not_found(
        request: Request, exc: TicketNotFoundError
    ) -> JSONResponse:
        body = ErrorResponse(
            error=ErrorDetail(
                code="ticket_not_found",
                message=f"Ticket {exc.ticket_id} was not found.",
            ),
            meta=ResponseMeta(request_id=request.state.request_id),
        )
        return JSONResponse(status_code=404, content=body.model_dump(mode="json"))

    @application.exception_handler(InvestigationJobNotFoundError)
    async def handle_investigation_job_not_found(
        request: Request, exc: InvestigationJobNotFoundError
    ) -> JSONResponse:
        body = ErrorResponse(
            error=ErrorDetail(
                code="investigation_job_not_found",
                message=f"Investigation job {exc.job_id} was not found.",
            ),
            meta=ResponseMeta(request_id=request.state.request_id),
        )
        return JSONResponse(
            status_code=404,
            content=body.model_dump(mode="json"),
        )

    @application.exception_handler(IdempotencyConflictError)
    async def handle_idempotency_conflict(
        request: Request, exc: IdempotencyConflictError
    ) -> JSONResponse:
        body = ErrorResponse(
            error=ErrorDetail(
                code="idempotency_conflict",
                message="The idempotency key was already used for different input.",
            ),
            meta=ResponseMeta(request_id=request.state.request_id),
        )
        return JSONResponse(status_code=409, content=body.model_dump(mode="json"))

    @application.exception_handler(ConcurrentTicketUpdateError)
    async def handle_concurrent_ticket_update(
        request: Request, exc: ConcurrentTicketUpdateError
    ) -> JSONResponse:
        body = ErrorResponse(
            error=ErrorDetail(
                code="concurrent_ticket_update",
                message="The ticket changed after the supplied version was read.",
                details=[{"expected_version": exc.expected_version}],
            ),
            meta=ResponseMeta(request_id=request.state.request_id),
        )
        return JSONResponse(status_code=409, content=body.model_dump(mode="json"))

    @application.exception_handler(ConcurrentJobUpdateError)
    async def handle_concurrent_job_update(
        request: Request, exc: ConcurrentJobUpdateError
    ) -> JSONResponse:
        body = ErrorResponse(
            error=ErrorDetail(
                code="concurrent_job_update",
                message="The investigation job changed during this operation.",
            ),
            meta=ResponseMeta(request_id=request.state.request_id),
        )
        return JSONResponse(
            status_code=409,
            content=body.model_dump(mode="json"),
        )

    @application.exception_handler(InvalidTicketTransitionError)
    async def handle_invalid_ticket_transition(
        request: Request, exc: InvalidTicketTransitionError
    ) -> JSONResponse:
        body = ErrorResponse(
            error=ErrorDetail(
                code="invalid_ticket_transition",
                message="The requested ticket status transition is not allowed.",
                details=[
                    {
                        "current_status": exc.current_status.value,
                        "target_status": exc.target_status.value,
                        "allowed_statuses": sorted(
                            status.value for status in exc.allowed_statuses
                        ),
                    }
                ],
            ),
            meta=ResponseMeta(request_id=request.state.request_id),
        )
        return JSONResponse(status_code=409, content=body.model_dump(mode="json"))

    @application.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        validation_details = [
            {
                "location": [str(part) for part in error["loc"]],
                "message": error["msg"],
                "type": error["type"],
            }
            for error in exc.errors()
        ]
        body = ErrorResponse(
            error=ErrorDetail(
                code="validation_error",
                message="Request validation failed.",
                details=validation_details,
            ),
            meta=ResponseMeta(request_id=request.state.request_id),
        )
        return JSONResponse(status_code=422, content=body.model_dump(mode="json"))

    @application.exception_handler(MemoryIdempotencyConflictError)
    async def handle_memory_idempotency_conflict(
        request: Request,
        exc: MemoryIdempotencyConflictError,
    ) -> JSONResponse:
        body = ErrorResponse(
            error=ErrorDetail(
                code=exc.code,
                message="The memory idempotency key has different input.",
            ),
            meta=ResponseMeta(request_id=request.state.request_id),
        )
        return JSONResponse(status_code=409, content=body.model_dump(mode="json"))

    @application.exception_handler(MemoryStorageError)
    @application.exception_handler(MemoryReadError)
    async def handle_memory_storage_error(
        request: Request,
        exc: MemoryStorageError | MemoryReadError,
    ) -> JSONResponse:
        body = ErrorResponse(
            error=ErrorDetail(
                code=exc.code,
                message="Long-term memory storage is temporarily unavailable.",
            ),
            meta=ResponseMeta(request_id=request.state.request_id),
        )
        return JSONResponse(status_code=503, content=body.model_dump(mode="json"))

    @application.exception_handler(MemoryPolicyValidationError)
    async def handle_memory_policy_validation(
        request: Request,
        exc: MemoryPolicyValidationError,
    ) -> JSONResponse:
        body = ErrorResponse(
            error=ErrorDetail(code=exc.code, message=str(exc)),
            meta=ResponseMeta(request_id=request.state.request_id),
        )
        return JSONResponse(status_code=422, content=body.model_dump(mode="json"))
