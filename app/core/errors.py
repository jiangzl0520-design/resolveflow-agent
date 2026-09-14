from uuid import UUID


class TicketNotFoundError(Exception):
    """Raised when a requested ticket does not exist."""

    def __init__(self, ticket_id: UUID) -> None:
        self.ticket_id = ticket_id
        super().__init__(f"Ticket {ticket_id} was not found.")


class StorageUnavailableError(Exception):
    """Raised when durable storage cannot complete an operation safely."""


class IdempotencyConflictError(Exception):
    """Raised when one idempotency key is reused for different input."""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"Idempotency key {key!r} has different input.")


class DuplicateIdempotencyKeyError(Exception):
    """Internal race signal raised when another request owns the key."""


class ConcurrentTicketUpdateError(Exception):
    """Raised when a ticket no longer has the caller's expected version."""

    def __init__(self, ticket_id: UUID, expected_version: int) -> None:
        self.ticket_id = ticket_id
        self.expected_version = expected_version
        super().__init__(
            f"Ticket {ticket_id} is no longer at version {expected_version}."
        )


class AuthenticationError(Exception):
    """Raised when a bearer token cannot establish a trusted identity."""


class AuthorizationDeniedError(Exception):
    """Raised when an authenticated actor lacks an operation permission."""

    def __init__(self, permission: str) -> None:
        self.permission = permission
        super().__init__(f"Permission {permission!r} was denied.")


class InvestigationJobNotFoundError(Exception):
    """Raised when a tenant-scoped investigation job is not visible."""

    def __init__(self, job_id: UUID) -> None:
        self.job_id = job_id
        super().__init__(f"Investigation job {job_id} was not found.")


class DuplicateJobIdempotencyKeyError(Exception):
    """Internal race signal for concurrent duplicate job submissions."""


class ConcurrentJobUpdateError(Exception):
    """Raised when a job version or worker lease is no longer current."""

    def __init__(self, job_id: UUID, expected_version: int) -> None:
        self.job_id = job_id
        self.expected_version = expected_version
        super().__init__(
            f"Job {job_id} is no longer at version {expected_version}."
        )


class BrokerUnavailableError(Exception):
    """Raised when a durable job cannot currently be sent to the broker."""
