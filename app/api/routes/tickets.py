from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response, status

from app.api.dependencies import (
    get_current_actor,
    get_request_id,
    get_ticket_service,
)
from app.domain.auth import AuthenticatedActor
from app.schemas.common import ResponseMeta
from app.schemas.ticket import (
    TicketCreate,
    TicketEventListResponse,
    TicketEventRead,
    TicketRead,
    TicketResponse,
    TicketStatusTransition,
)
from app.services.ticket_service import (
    CreateTicketCommand,
    TicketService,
    TransitionTicketStatusCommand,
)

router = APIRouter(prefix="/tickets", tags=["tickets"])
TicketServiceDependency = Annotated[TicketService, Depends(get_ticket_service)]
ActorDependency = Annotated[
    AuthenticatedActor,
    Depends(get_current_actor),
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


@router.post("", response_model=TicketResponse, status_code=status.HTTP_201_CREATED)
def create_ticket(
    payload: TicketCreate,
    request: Request,
    response: Response,
    actor: ActorDependency,
    service: TicketServiceDependency,
    idempotency_key: IdempotencyKey,
) -> TicketResponse:
    result = service.create_ticket(
        actor,
        CreateTicketCommand(
            customer_id=payload.customer_id,
            subject=payload.subject,
            description=payload.description,
            category=payload.category,
            idempotency_key=idempotency_key,
        ),
        request_id=get_request_id(request),
    )
    response.headers["Idempotency-Replayed"] = str(result.replayed).lower()
    return TicketResponse(
        data=TicketRead.model_validate(result.ticket),
        meta=ResponseMeta(request_id=get_request_id(request)),
    )


@router.get("/{ticket_id}", response_model=TicketResponse)
def get_ticket(
    ticket_id: UUID,
    request: Request,
    actor: ActorDependency,
    service: TicketServiceDependency,
) -> TicketResponse:
    ticket = service.get_ticket(
        actor,
        ticket_id,
        request_id=get_request_id(request),
    )
    return TicketResponse(
        data=TicketRead.model_validate(ticket),
        meta=ResponseMeta(request_id=get_request_id(request)),
    )


@router.patch("/{ticket_id}/status", response_model=TicketResponse)
def transition_ticket_status(
    ticket_id: UUID,
    payload: TicketStatusTransition,
    request: Request,
    actor: ActorDependency,
    service: TicketServiceDependency,
) -> TicketResponse:
    ticket = service.transition_status(
        actor,
        TransitionTicketStatusCommand(
            ticket_id=ticket_id,
            target_status=payload.target_status,
            expected_version=payload.expected_version,
        ),
        request_id=get_request_id(request),
    )
    return TicketResponse(
        data=TicketRead.model_validate(ticket),
        meta=ResponseMeta(request_id=get_request_id(request)),
    )


@router.get("/{ticket_id}/events", response_model=TicketEventListResponse)
def list_ticket_events(
    ticket_id: UUID,
    request: Request,
    actor: ActorDependency,
    service: TicketServiceDependency,
) -> TicketEventListResponse:
    events = service.list_events(
        actor,
        ticket_id,
        request_id=get_request_id(request),
    )
    return TicketEventListResponse(
        data=[TicketEventRead.model_validate(event) for event in events],
        meta=ResponseMeta(request_id=get_request_id(request)),
    )
