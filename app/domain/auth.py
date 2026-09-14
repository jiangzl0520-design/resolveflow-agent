from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class Role(StrEnum):
    CUSTOMER = "customer"
    AGENT = "agent"
    SUPERVISOR = "supervisor"
    TENANT_ADMIN = "tenant_admin"


class Permission(StrEnum):
    TICKET_CREATE = "ticket:create"
    TICKET_READ = "ticket:read"
    TICKET_TRANSITION = "ticket:transition"
    TICKET_EVENTS_READ = "ticket_events:read"
    AUTHORIZATION_AUDIT_READ = "authorization_audit:read"
    INVESTIGATION_JOB_SUBMIT = "investigation_job:submit"
    INVESTIGATION_JOB_READ = "investigation_job:read"
    INVESTIGATION_JOB_CANCEL = "investigation_job:cancel"
    INVESTIGATION_JOB_EVENTS_READ = "investigation_job_events:read"
    TOOL_ORDER_READ = "tool:order:read"
    TOOL_LOGISTICS_READ = "tool:logistics:read"
    TOOL_POLICY_READ = "tool:policy:read"
    REFUND_APPROVE = "refund:approve"
    TOOL_REFUND_EXECUTE = "tool:refund:execute"
    TOOL_REFUND_STATUS_READ = "tool:refund_status:read"
    MEMORY_READ = "memory:read"
    MEMORY_WRITE = "memory:write"
    MEMORY_DELETE = "memory:delete"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.CUSTOMER: frozenset(
        {
            Permission.TICKET_CREATE,
            Permission.TICKET_READ,
            Permission.TICKET_EVENTS_READ,
            Permission.MEMORY_READ,
            Permission.MEMORY_WRITE,
            Permission.MEMORY_DELETE,
        }
    ),
    Role.AGENT: frozenset(
        {
            Permission.TICKET_CREATE,
            Permission.TICKET_READ,
            Permission.TICKET_TRANSITION,
            Permission.TICKET_EVENTS_READ,
            Permission.INVESTIGATION_JOB_SUBMIT,
            Permission.INVESTIGATION_JOB_READ,
            Permission.INVESTIGATION_JOB_CANCEL,
            Permission.INVESTIGATION_JOB_EVENTS_READ,
            Permission.TOOL_ORDER_READ,
            Permission.TOOL_LOGISTICS_READ,
            Permission.TOOL_POLICY_READ,
            Permission.MEMORY_READ,
            Permission.MEMORY_WRITE,
        }
    ),
    Role.SUPERVISOR: frozenset(
        {
            Permission.TICKET_CREATE,
            Permission.TICKET_READ,
            Permission.TICKET_TRANSITION,
            Permission.TICKET_EVENTS_READ,
            Permission.AUTHORIZATION_AUDIT_READ,
            Permission.INVESTIGATION_JOB_SUBMIT,
            Permission.INVESTIGATION_JOB_READ,
            Permission.INVESTIGATION_JOB_CANCEL,
            Permission.INVESTIGATION_JOB_EVENTS_READ,
            Permission.TOOL_ORDER_READ,
            Permission.TOOL_LOGISTICS_READ,
            Permission.TOOL_POLICY_READ,
            Permission.REFUND_APPROVE,
            Permission.TOOL_REFUND_EXECUTE,
            Permission.TOOL_REFUND_STATUS_READ,
            Permission.MEMORY_READ,
            Permission.MEMORY_WRITE,
            Permission.MEMORY_DELETE,
        }
    ),
    Role.TENANT_ADMIN: frozenset(Permission),
}

STAFF_ROLES = frozenset(
    {
        Role.AGENT,
        Role.SUPERVISOR,
        Role.TENANT_ADMIN,
    }
)


@dataclass(frozen=True, slots=True)
class AuthenticatedActor:
    actor_id: str
    tenant_id: UUID
    roles: frozenset[Role]

    def has_permission(self, permission: Permission) -> bool:
        return any(
            permission in ROLE_PERMISSIONS[role]
            for role in self.roles
        )

    @property
    def is_staff(self) -> bool:
        return bool(self.roles & STAFF_ROLES)
