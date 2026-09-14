from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.domain.auth import Permission


class AuthorizationDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class AuthorizationAuditEvent:
    id: UUID
    tenant_id: UUID
    actor_id: str
    permission: Permission
    decision: AuthorizationDecision
    reason: str
    request_id: str
    resource_type: str
    resource_id: str | None
    created_at: datetime
