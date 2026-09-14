from threading import RLock
from typing import Protocol
from uuid import UUID

from app.domain.authorization_audit import AuthorizationAuditEvent


class AuthorizationAuditRepository(Protocol):
    def add(self, event: AuthorizationAuditEvent) -> None: ...

    def list_for_tenant(
        self,
        tenant_id: UUID,
        *,
        limit: int,
    ) -> list[AuthorizationAuditEvent]: ...


class InMemoryAuthorizationAuditRepository:
    def __init__(self) -> None:
        self._events: list[AuthorizationAuditEvent] = []
        self._lock = RLock()

    def add(self, event: AuthorizationAuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    def list_for_tenant(
        self,
        tenant_id: UUID,
        *,
        limit: int,
    ) -> list[AuthorizationAuditEvent]:
        with self._lock:
            scoped = [
                event
                for event in reversed(self._events)
                if event.tenant_id == tenant_id
            ]
            return scoped[:limit]
