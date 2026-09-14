from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from app.domain.auth import Permission, Role
from app.domain.authorization_audit import AuthorizationDecision
from app.schemas.common import ResponseMeta

ActorId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]


class DevelopmentTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor_id: ActorId
    tenant_id: UUID
    roles: set[Role] = Field(min_length=1)

    @field_validator("tenant_id")
    @classmethod
    def reject_legacy_quarantine_tenant(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError(
                "The legacy quarantine tenant cannot authenticate."
            )
        return value


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class AuthorizationAuditRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    actor_id: str
    permission: Permission
    decision: AuthorizationDecision
    reason: str
    request_id: str
    resource_type: str
    resource_id: str | None
    created_at: datetime


class AuthorizationAuditListResponse(BaseModel):
    data: list[AuthorizationAuditRead]
    meta: ResponseMeta
