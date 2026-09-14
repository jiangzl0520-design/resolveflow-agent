from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.core.errors import (
    AuthorizationDeniedError,
    InvestigationJobNotFoundError,
    TicketNotFoundError,
)
from app.domain.auth import AuthenticatedActor, Permission
from app.domain.authorization_audit import (
    AuthorizationAuditEvent,
    AuthorizationDecision,
)
from app.domain.ticket import Ticket
from app.domain.investigation_job import InvestigationJob
from app.repositories.authorization_audit_repository import (
    AuthorizationAuditRepository,
)


class AuthorizationService:
    """Apply deterministic RBAC/resource rules and persist each decision."""

    def __init__(self, audit_repository: AuthorizationAuditRepository) -> None:
        self._audit_repository = audit_repository

    def require_ticket_create(
        self,
        actor: AuthenticatedActor,
        *,
        customer_id: str,
        request_id: str,
    ) -> None:
        permission = Permission.TICKET_CREATE
        if not actor.has_permission(permission):
            self._deny(
                actor,
                permission,
                reason="role_missing_permission",
                request_id=request_id,
                resource_type="ticket",
            )
        if not actor.is_staff and customer_id != actor.actor_id:
            self._deny(
                actor,
                permission,
                reason="customer_identity_mismatch",
                request_id=request_id,
                resource_type="ticket",
            )
        self._allow(
            actor,
            permission,
            reason=(
                "staff_role"
                if actor.is_staff
                else "customer_identity_match"
            ),
            request_id=request_id,
            resource_type="ticket",
        )

    def require_ticket_access(
        self,
        actor: AuthenticatedActor,
        permission: Permission,
        *,
        ticket_id: UUID,
        ticket: Ticket | None,
        request_id: str,
    ) -> Ticket:
        if not actor.has_permission(permission):
            self._deny(
                actor,
                permission,
                reason="role_missing_permission",
                request_id=request_id,
                resource_type="ticket",
                resource_id=str(ticket_id),
            )
        if ticket is None:
            self._record(
                actor,
                permission,
                decision=AuthorizationDecision.DENY,
                reason="not_found_or_out_of_tenant_scope",
                request_id=request_id,
                resource_type="ticket",
                resource_id=str(ticket_id),
            )
            raise TicketNotFoundError(ticket_id)
        if not actor.is_staff and ticket.customer_id != actor.actor_id:
            self._record(
                actor,
                permission,
                decision=AuthorizationDecision.DENY,
                reason="not_found_or_not_owned",
                request_id=request_id,
                resource_type="ticket",
                resource_id=str(ticket_id),
            )
            raise TicketNotFoundError(ticket_id)
        self._allow(
            actor,
            permission,
            reason="staff_role" if actor.is_staff else "resource_owner",
            request_id=request_id,
            resource_type="ticket",
            resource_id=str(ticket_id),
        )
        return ticket

    def require_audit_read(
        self,
        actor: AuthenticatedActor,
        *,
        request_id: str,
    ) -> None:
        permission = Permission.AUTHORIZATION_AUDIT_READ
        if not actor.has_permission(permission):
            self._deny(
                actor,
                permission,
                reason="role_missing_permission",
                request_id=request_id,
                resource_type="authorization_audit",
            )
        self._allow(
            actor,
            permission,
            reason="role_permission",
            request_id=request_id,
            resource_type="authorization_audit",
        )

    def require_subject_memory_access(
        self,
        actor: AuthenticatedActor,
        permission: Permission,
        *,
        subject_id: str,
        request_id: str,
    ) -> None:
        if permission not in {
            Permission.MEMORY_READ,
            Permission.MEMORY_WRITE,
            Permission.MEMORY_DELETE,
        }:
            raise ValueError("Expected a memory permission.")
        if not actor.has_permission(permission):
            self._deny(
                actor,
                permission,
                reason="role_missing_permission",
                request_id=request_id,
                resource_type="long_term_memory",
                resource_id=subject_id,
            )
        if not actor.is_staff and actor.actor_id != subject_id:
            self._deny(
                actor,
                permission,
                reason="memory_subject_identity_mismatch",
                request_id=request_id,
                resource_type="long_term_memory",
                resource_id=subject_id,
            )
        self._allow(
            actor,
            permission,
            reason=("staff_role" if actor.is_staff else "subject_owner"),
            request_id=request_id,
            resource_type="long_term_memory",
            resource_id=subject_id,
        )

    def require_investigation_job_access(
        self,
        actor: AuthenticatedActor,
        permission: Permission,
        *,
        job_id: UUID,
        job: InvestigationJob | None,
        request_id: str,
    ) -> InvestigationJob:
        if not actor.has_permission(permission):
            self._deny(
                actor,
                permission,
                reason="role_missing_permission",
                request_id=request_id,
                resource_type="investigation_job",
                resource_id=str(job_id),
            )
        if job is None:
            self._record(
                actor,
                permission,
                decision=AuthorizationDecision.DENY,
                reason="not_found_or_out_of_tenant_scope",
                request_id=request_id,
                resource_type="investigation_job",
                resource_id=str(job_id),
            )
            raise InvestigationJobNotFoundError(job_id)
        self._allow(
            actor,
            permission,
            reason="tenant_role_permission",
            request_id=request_id,
            resource_type="investigation_job",
            resource_id=str(job_id),
        )
        return job

    def list_audit_events(
        self,
        actor: AuthenticatedActor,
        *,
        request_id: str,
        limit: int,
    ) -> list[AuthorizationAuditEvent]:
        self.require_audit_read(actor, request_id=request_id)
        return self._audit_repository.list_for_tenant(
            actor.tenant_id,
            limit=limit,
        )

    def _allow(
        self,
        actor: AuthenticatedActor,
        permission: Permission,
        *,
        reason: str,
        request_id: str,
        resource_type: str,
        resource_id: str | None = None,
    ) -> None:
        self._record(
            actor,
            permission,
            decision=AuthorizationDecision.ALLOW,
            reason=reason,
            request_id=request_id,
            resource_type=resource_type,
            resource_id=resource_id,
        )

    def _deny(
        self,
        actor: AuthenticatedActor,
        permission: Permission,
        *,
        reason: str,
        request_id: str,
        resource_type: str,
        resource_id: str | None = None,
    ) -> None:
        self._record(
            actor,
            permission,
            decision=AuthorizationDecision.DENY,
            reason=reason,
            request_id=request_id,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        raise AuthorizationDeniedError(permission.value)

    def _record(
        self,
        actor: AuthenticatedActor,
        permission: Permission,
        *,
        decision: AuthorizationDecision,
        reason: str,
        request_id: str,
        resource_type: str,
        resource_id: str | None = None,
    ) -> None:
        self._audit_repository.add(
            AuthorizationAuditEvent(
                id=uuid4(),
                tenant_id=actor.tenant_id,
                actor_id=actor.actor_id,
                permission=permission,
                decision=decision,
                reason=reason,
                request_id=request_id,
                resource_type=resource_type,
                resource_id=resource_id,
                created_at=datetime.now(UTC),
            )
        )
