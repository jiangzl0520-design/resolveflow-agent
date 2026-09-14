from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.domain.ticket import TicketCategory, TicketStatus
from app.domain.ticket_event import TicketActorType, TicketEventType
from app.schemas.common import ResponseMeta

CustomerId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
]
Subject = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
Description = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2000),
]


class TicketCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: CustomerId
    subject: Subject
    description: Description
    category: TicketCategory


class TicketRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    customer_id: str
    subject: str
    description: str
    category: TicketCategory
    status: TicketStatus
    created_at: datetime
    updated_at: datetime
    version: int


class TicketStatusTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_status: TicketStatus
    expected_version: int = Field(ge=1)


class TicketEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ticket_id: UUID
    event_type: TicketEventType
    actor_type: TicketActorType
    actor_id: str
    payload: dict[str, Any]
    created_at: datetime


class TicketResponse(BaseModel):
    data: TicketRead
    meta: ResponseMeta


class TicketEventListResponse(BaseModel):
    data: list[TicketEventRead]
    meta: ResponseMeta
