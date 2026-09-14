from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from app.api.dependencies import (
    get_current_actor,
    get_memory_service,
    get_request_id,
    get_trace_id,
)
from app.domain.auth import AuthenticatedActor
from app.domain.memory import MemoryDeleteCommand, MemoryWriteCommand
from app.schemas.common import ResponseMeta
from app.schemas.memory import (
    MemoryCreate,
    MemoryDelete,
    MemoryListResponse,
    MemoryOperationRead,
    MemoryOperationResponse,
    MemoryRead,
)
from app.services.memory_service import MemoryService

router = APIRouter(tags=["memories"])
MemoryServiceDependency = Annotated[
    MemoryService,
    Depends(get_memory_service),
]
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


@router.post("/memories", response_model=MemoryOperationResponse)
def remember(
    payload: MemoryCreate,
    request: Request,
    actor: ActorDependency,
    service: MemoryServiceDependency,
    idempotency_key: IdempotencyKey,
) -> MemoryOperationResponse:
    result = service.remember(
        actor,
        MemoryWriteCommand(
            **payload.model_dump(),
            idempotency_key=idempotency_key,
            request_id=get_request_id(request),
            trace_id=get_trace_id(request),
        ),
    )
    return MemoryOperationResponse(
        data=MemoryOperationRead(
            decision=result.decision,
            memory_id=result.memory_id,
            memory_key=result.memory_key,
        ),
        meta=ResponseMeta(request_id=get_request_id(request)),
    )


@router.get(
    "/memory-subjects/{subject_id}/memories",
    response_model=MemoryListResponse,
)
def recall(
    subject_id: str,
    request: Request,
    actor: ActorDependency,
    service: MemoryServiceDependency,
) -> MemoryListResponse:
    memories = service.recall_for_subject(
        actor,
        subject_id,
        request_id=get_request_id(request),
    )
    return MemoryListResponse(
        data=[
            MemoryRead.model_validate(
                {
                    "id": memory.id,
                    "memory_key": memory.memory_key,
                    "category": memory.category,
                    "value": memory.value,
                    "source_type": memory.source_type,
                    "confidence": memory.confidence,
                    "observed_at": memory.observed_at,
                    "expires_at": memory.expires_at,
                    "version": memory.version,
                }
            )
            for memory in memories
        ],
        meta=ResponseMeta(request_id=get_request_id(request)),
    )


@router.delete(
    "/memory-subjects/{subject_id}/memories/{memory_key}",
    response_model=MemoryOperationResponse,
)
def forget(
    subject_id: str,
    memory_key: str,
    payload: MemoryDelete,
    request: Request,
    actor: ActorDependency,
    service: MemoryServiceDependency,
    idempotency_key: IdempotencyKey,
) -> MemoryOperationResponse:
    result = service.forget(
        actor,
        MemoryDeleteCommand(
            subject_id=subject_id,
            memory_key=memory_key,
            reason=payload.reason,
            idempotency_key=idempotency_key,
            request_id=get_request_id(request),
            trace_id=get_trace_id(request),
        ),
    )
    return MemoryOperationResponse(
        data=MemoryOperationRead(
            decision=result.decision,
            memory_id=result.memory_id,
            memory_key=result.memory_key,
        ),
        meta=ResponseMeta(request_id=get_request_id(request)),
    )
