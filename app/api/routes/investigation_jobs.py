from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response, status

from app.api.dependencies import (
    get_current_actor,
    get_investigation_job_service,
    get_request_id,
    get_trace_id,
)
from app.domain.auth import AuthenticatedActor
from app.schemas.common import ResponseMeta
from app.schemas.investigation_job import (
    InvestigationJobEventListResponse,
    InvestigationJobEventRead,
    InvestigationJobRead,
    InvestigationJobResponse,
)
from app.services.investigation_job_service import (
    InvestigationJobService,
    SubmitInvestigationJobCommand,
)

router = APIRouter(tags=["investigation-jobs"])
ActorDependency = Annotated[
    AuthenticatedActor,
    Depends(get_current_actor),
]
JobServiceDependency = Annotated[
    InvestigationJobService,
    Depends(get_investigation_job_service),
]
IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    ),
]


@router.post(
    "/tickets/{ticket_id}/investigation-jobs",
    response_model=InvestigationJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def submit_investigation_job(
    ticket_id: UUID,
    request: Request,
    response: Response,
    actor: ActorDependency,
    service: JobServiceDependency,
    idempotency_key: IdempotencyKey,
) -> InvestigationJobResponse:
    result = service.submit(
        actor,
        SubmitInvestigationJobCommand(
            ticket_id=ticket_id,
            idempotency_key=idempotency_key,
        ),
        request_id=get_request_id(request),
        trace_id=get_trace_id(request),
    )
    response.headers["Location"] = (
        f"/api/v1/investigation-jobs/{result.job.id}"
    )
    response.headers["Idempotency-Replayed"] = str(
        result.replayed
    ).lower()
    response.headers["Dispatch-Pending"] = str(
        result.dispatch_pending
    ).lower()
    return InvestigationJobResponse(
        data=InvestigationJobRead.model_validate(result.job),
        meta=ResponseMeta(request_id=get_request_id(request)),
    )


@router.get(
    "/investigation-jobs/{job_id}",
    response_model=InvestigationJobResponse,
)
def get_investigation_job(
    job_id: UUID,
    request: Request,
    actor: ActorDependency,
    service: JobServiceDependency,
) -> InvestigationJobResponse:
    job = service.get(
        actor,
        job_id,
        request_id=get_request_id(request),
    )
    return InvestigationJobResponse(
        data=InvestigationJobRead.model_validate(job),
        meta=ResponseMeta(request_id=get_request_id(request)),
    )


@router.post(
    "/investigation-jobs/{job_id}/cancel",
    response_model=InvestigationJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def cancel_investigation_job(
    job_id: UUID,
    request: Request,
    actor: ActorDependency,
    service: JobServiceDependency,
) -> InvestigationJobResponse:
    job = service.cancel(
        actor,
        job_id,
        request_id=get_request_id(request),
    )
    return InvestigationJobResponse(
        data=InvestigationJobRead.model_validate(job),
        meta=ResponseMeta(request_id=get_request_id(request)),
    )


@router.get(
    "/investigation-jobs/{job_id}/events",
    response_model=InvestigationJobEventListResponse,
)
def list_investigation_job_events(
    job_id: UUID,
    request: Request,
    actor: ActorDependency,
    service: JobServiceDependency,
) -> InvestigationJobEventListResponse:
    events = service.list_events(
        actor,
        job_id,
        request_id=get_request_id(request),
    )
    return InvestigationJobEventListResponse(
        data=[
            InvestigationJobEventRead.model_validate(event)
            for event in events
        ],
        meta=ResponseMeta(request_id=get_request_id(request)),
    )
