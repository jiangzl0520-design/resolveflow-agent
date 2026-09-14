from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
from uuid import UUID, uuid4

from app.core.errors import (
    ConcurrentTicketUpdateError,
    DuplicateIdempotencyKeyError,
    IdempotencyConflictError,
    StorageUnavailableError,
    TicketNotFoundError,
)
from app.domain.idempotency import IdempotencyRecord
from app.domain.auth import AuthenticatedActor, Permission
from app.domain.ticket import Ticket, TicketCategory, TicketStatus
from app.domain.ticket_event import (
    TicketActorType,
    TicketEvent,
    TicketEventType,
)
from app.services.unit_of_work import (
    TicketUnitOfWork,
    TicketUnitOfWorkFactory,
)
from app.services.authorization_service import AuthorizationService

CREATE_TICKET_OPERATION = "create_ticket"


@dataclass(frozen=True, slots=True)
class CreateTicketCommand:
    customer_id: str
    subject: str
    description: str
    category: TicketCategory
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class CreateTicketResult:
    ticket: Ticket
    replayed: bool


@dataclass(frozen=True, slots=True)
class TransitionTicketStatusCommand:
    ticket_id: UUID
    target_status: TicketStatus
    expected_version: int


class TicketService:
    def __init__(
        self,
        unit_of_work_factory: TicketUnitOfWorkFactory,
        authorization_service: AuthorizationService,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._authorization = authorization_service

    def create_ticket(
        self,
        actor: AuthenticatedActor,
        command: CreateTicketCommand,
        *,
        request_id: str,
    ) -> CreateTicketResult:
        self._authorization.require_ticket_create(
            actor,
            customer_id=command.customer_id,
            request_id=request_id,
        )
        request_hash = _create_ticket_request_hash(command)
        try:
            return self._create_ticket_once(actor, command, request_hash)
        except DuplicateIdempotencyKeyError:
            return self._resolve_idempotent_replay(
                actor,
                command.idempotency_key,
                request_hash,
            )

    def _create_ticket_once(
        self,
        actor: AuthenticatedActor,
        command: CreateTicketCommand,
        request_hash: str,
    ) -> CreateTicketResult:
        with self._unit_of_work_factory(actor.tenant_id) as unit_of_work:
            existing = unit_of_work.idempotency.get(
                CREATE_TICKET_OPERATION,
                command.idempotency_key,
            )
            if existing is not None:
                return self._replay_from_record(
                    unit_of_work,
                    existing,
                    request_hash,
                )

            now = datetime.now(UTC)
            ticket = Ticket(
                id=uuid4(),
                tenant_id=actor.tenant_id,
                customer_id=command.customer_id,
                subject=command.subject,
                description=command.description,
                category=command.category,
                status=TicketStatus.OPEN,
                created_at=now,
                updated_at=now,
            )
            event = TicketEvent(
                id=uuid4(),
                tenant_id=actor.tenant_id,
                ticket_id=ticket.id,
                event_type=TicketEventType.CREATED,
                actor_type=(
                    TicketActorType.STAFF
                    if actor.is_staff
                    else TicketActorType.CUSTOMER
                ),
                actor_id=actor.actor_id,
                payload={
                    "status": ticket.status.value,
                    "category": ticket.category.value,
                    "version": ticket.version,
                },
                created_at=now,
            )
            idempotency_record = IdempotencyRecord(
                tenant_id=actor.tenant_id,
                operation=CREATE_TICKET_OPERATION,
                key=command.idempotency_key,
                request_hash=request_hash,
                resource_id=ticket.id,
                created_at=now,
            )

            unit_of_work.tickets.add(ticket)
            unit_of_work.flush()
            unit_of_work.events.add(event)
            unit_of_work.idempotency.add(idempotency_record)
            unit_of_work.commit()
            return CreateTicketResult(ticket=ticket, replayed=False)

    def _resolve_idempotent_replay(
        self,
        actor: AuthenticatedActor,
        idempotency_key: str,
        request_hash: str,
    ) -> CreateTicketResult:
        with self._unit_of_work_factory(actor.tenant_id) as unit_of_work:
            record = unit_of_work.idempotency.get(
                CREATE_TICKET_OPERATION,
                idempotency_key,
            )
            if record is None:
                raise StorageUnavailableError(
                    "Idempotency winner could not be loaded."
                )
            return self._replay_from_record(
                unit_of_work,
                record,
                request_hash,
            )

    @staticmethod
    def _replay_from_record(
        unit_of_work: TicketUnitOfWork,
        record: IdempotencyRecord,
        request_hash: str,
    ) -> CreateTicketResult:
        if record.request_hash != request_hash:
            raise IdempotencyConflictError(record.key)
        ticket = unit_of_work.tickets.get(record.resource_id)
        if ticket is None:
            raise StorageUnavailableError(
                "Idempotency record references a missing ticket."
            )
        return CreateTicketResult(ticket=ticket, replayed=True)

    def get_ticket(
        self,
        actor: AuthenticatedActor,
        ticket_id: UUID,
        *,
        request_id: str,
    ) -> Ticket:
        with self._unit_of_work_factory(actor.tenant_id) as unit_of_work:
            ticket = unit_of_work.tickets.get(ticket_id)
        return self._authorization.require_ticket_access(
            actor,
            Permission.TICKET_READ,
            ticket_id=ticket_id,
            ticket=ticket,
            request_id=request_id,
        )

    def transition_status(
        self,
        actor: AuthenticatedActor,
        command: TransitionTicketStatusCommand,
        *,
        request_id: str,
    ) -> Ticket:
        with self._unit_of_work_factory(actor.tenant_id) as unit_of_work:
            ticket = unit_of_work.tickets.get(command.ticket_id)
        self._authorization.require_ticket_access(
            actor,
            Permission.TICKET_TRANSITION,
            ticket_id=command.ticket_id,
            ticket=ticket,
            request_id=request_id,
        )

        with self._unit_of_work_factory(actor.tenant_id) as unit_of_work:
            ticket = unit_of_work.tickets.get(command.ticket_id)
            if ticket is None:
                raise TicketNotFoundError(command.ticket_id)
            if ticket.version != command.expected_version:
                raise ConcurrentTicketUpdateError(
                    ticket.id,
                    command.expected_version,
                )

            now = datetime.now(UTC)
            updated_ticket = ticket.transition_to(
                command.target_status,
                changed_at=now,
            )
            event = TicketEvent(
                id=uuid4(),
                tenant_id=actor.tenant_id,
                ticket_id=ticket.id,
                event_type=TicketEventType.STATUS_CHANGED,
                actor_type=(
                    TicketActorType.STAFF
                    if actor.is_staff
                    else TicketActorType.CUSTOMER
                ),
                actor_id=actor.actor_id,
                payload={
                    "from_status": ticket.status.value,
                    "to_status": updated_ticket.status.value,
                    "from_version": ticket.version,
                    "to_version": updated_ticket.version,
                },
                created_at=now,
            )

            unit_of_work.tickets.update(
                updated_ticket,
                expected_version=command.expected_version,
            )
            unit_of_work.events.add(event)
            unit_of_work.commit()
            return updated_ticket

    def list_events(
        self,
        actor: AuthenticatedActor,
        ticket_id: UUID,
        *,
        request_id: str,
    ) -> list[TicketEvent]:
        with self._unit_of_work_factory(actor.tenant_id) as unit_of_work:
            ticket = unit_of_work.tickets.get(ticket_id)
        self._authorization.require_ticket_access(
            actor,
            Permission.TICKET_EVENTS_READ,
            ticket_id=ticket_id,
            ticket=ticket,
            request_id=request_id,
        )
        with self._unit_of_work_factory(actor.tenant_id) as unit_of_work:
            return unit_of_work.events.list_for_ticket(ticket_id)


def _create_ticket_request_hash(command: CreateTicketCommand) -> str:
    canonical_payload = json.dumps(
        {
            "customer_id": command.customer_id,
            "subject": command.subject,
            "description": command.description,
            "category": command.category.value,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(canonical_payload).hexdigest()
