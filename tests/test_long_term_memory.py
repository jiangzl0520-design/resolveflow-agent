from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.core.errors import AuthorizationDeniedError
from app.domain.auth import AuthenticatedActor, Role
from app.domain.memory import (
    MemoryDecision,
    MemoryDeleteCommand,
    MemorySourceType,
    MemoryStatus,
    MemoryWriteCommand,
)
from app.memory.errors import MemoryIdempotencyConflictError
from app.repositories.authorization_audit_repository import (
    InMemoryAuthorizationAuditRepository,
)
from app.repositories.memory_repository import InMemoryMemoryRepository
from app.services.authorization_service import AuthorizationService
from app.services.memory_service import MemoryService

TENANT_ID = UUID("95000000-0000-0000-0000-000000000001")
OTHER_TENANT_ID = UUID("95000000-0000-0000-0000-000000000002")
NOW = datetime(2026, 8, 5, 9, 0, tzinfo=UTC)


def _actor(
    role: Role = Role.AGENT,
    *,
    actor_id: str = "memory-agent",
    tenant_id: UUID = TENANT_ID,
) -> AuthenticatedActor:
    return AuthenticatedActor(
        actor_id=actor_id,
        tenant_id=tenant_id,
        roles=frozenset({role}),
    )


def _service(
    repository: InMemoryMemoryRepository | None = None,
):
    resolved = repository or InMemoryMemoryRepository()
    audit = InMemoryAuthorizationAuditRepository()
    return (
        MemoryService(
            resolved,
            AuthorizationService(audit),
            clock=lambda: NOW,
        ),
        resolved,
        audit,
    )


def _write(
    *,
    value: str = "zh-CN",
    memory_key: str = "preference.language",
    source_type: MemorySourceType = MemorySourceType.EXPLICIT_USER,
    confidence: float = 0.99,
    observed_at: datetime = NOW,
    expires_at: datetime = NOW + timedelta(days=30),
    idempotency_key: str = "memory-write-1",
    subject_id: str = "customer-001",
) -> MemoryWriteCommand:
    return MemoryWriteCommand(
        subject_id=subject_id,
        memory_key=memory_key,
        value=value,
        source_type=source_type,
        source_reference="message-001",
        confidence=confidence,
        observed_at=observed_at,
        expires_at=expires_at,
        idempotency_key=idempotency_key,
        request_id=f"request-{idempotency_key}",
        trace_id=f"trace-{idempotency_key}",
    )


def test_explicit_stable_preference_is_recalled_across_ticket_bindings() -> None:
    service, repository, _ = _service()
    ticket_a = uuid4()
    ticket_b = uuid4()
    repository.bind_ticket(TENANT_ID, ticket_a, "customer-001")
    repository.bind_ticket(TENANT_ID, ticket_b, "customer-001")

    result = service.remember(_actor(), _write())

    assert result.decision is MemoryDecision.CREATED
    recalled = service.recall_for_subject(
        _actor(),
        "customer-001",
        request_id="recall-1",
    )
    assert len(recalled) == 1
    assert recalled[0].value == "zh-cn"
    assert repository.list_active_for_ticket(
        TENANT_ID,
        ticket_a,
        at=NOW,
    ) == recalled
    assert repository.list_active_for_ticket(
        TENANT_ID,
        ticket_b,
        at=NOW,
    ) == recalled


def test_model_inference_requires_confirmation_and_business_state_is_rejected() -> None:
    service, repository, _ = _service()

    inferred = service.remember(
        _actor(),
        _write(source_type=MemorySourceType.MODEL_INFERENCE),
    )
    prohibited = service.remember(
        _actor(),
        _write(
            memory_key="order.refund_eligible",
            value="true",
            idempotency_key="memory-write-prohibited",
        ),
    )

    assert inferred.decision is MemoryDecision.CONFIRMATION_REQUIRED
    assert prohibited.decision is MemoryDecision.REJECTED
    assert repository.memories == []
    assert len(repository.events) == 2


def test_confidence_and_ttl_thresholds_reject_unsafe_candidates() -> None:
    service, repository, _ = _service()

    low_confidence = service.remember(
        _actor(),
        _write(confidence=0.5, idempotency_key="low-confidence"),
    )
    excessive_ttl = service.remember(
        _actor(),
        _write(
            expires_at=NOW + timedelta(days=366),
            idempotency_key="excessive-ttl",
        ),
    )

    assert low_confidence.decision is MemoryDecision.REJECTED
    assert excessive_ttl.decision is MemoryDecision.REJECTED
    assert repository.memories == []


def test_newer_explicit_value_supersedes_but_ambiguous_older_value_conflicts() -> None:
    service, repository, _ = _service()
    service.remember(_actor(), _write())
    updated = service.remember(
        _actor(),
        _write(
            value="en-US",
            observed_at=NOW + timedelta(days=1),
            expires_at=NOW + timedelta(days=31),
            idempotency_key="newer-language",
        ),
    )

    assert updated.decision is MemoryDecision.UPDATED
    active = repository.list_active_for_subject(
        TENANT_ID,
        "customer-001",
        at=NOW,
    )
    assert [item.value for item in active] == ["en-us"]
    assert any(
        item.status is MemoryStatus.SUPERSEDED
        for item in repository.memories
    )

    conflict = service.remember(
        _actor(),
        _write(
            value="fr-FR",
            observed_at=NOW + timedelta(hours=12),
            expires_at=NOW + timedelta(days=30),
            idempotency_key="ambiguous-language",
        ),
    )
    assert conflict.decision is MemoryDecision.CONFLICTED
    assert repository.list_active_for_subject(
        TENANT_ID,
        "customer-001",
        at=NOW,
    ) == ()

    resolved = service.remember(
        _actor(),
        _write(
            value="de-DE",
            observed_at=NOW + timedelta(days=2),
            expires_at=NOW + timedelta(days=32),
            idempotency_key="resolved-language",
        ),
    )
    assert resolved.decision is MemoryDecision.UPDATED
    assert [
        item.value
        for item in repository.list_active_for_subject(
            TENANT_ID,
            "customer-001",
            at=NOW,
        )
    ] == ["de-de"]


def test_expired_memory_is_not_recalled() -> None:
    repository = InMemoryMemoryRepository()
    audit = InMemoryAuthorizationAuditRepository()
    service = MemoryService(
        repository,
        AuthorizationService(audit),
        clock=lambda: NOW,
    )
    service.remember(
        _actor(),
        _write(
            observed_at=NOW - timedelta(days=2),
            expires_at=NOW + timedelta(seconds=1),
        ),
    )

    assert repository.list_active_for_subject(
        TENANT_ID,
        "customer-001",
        at=NOW + timedelta(seconds=2),
    ) == ()


def test_delete_redacts_all_versions_and_is_idempotent() -> None:
    service, repository, _ = _service()
    service.remember(_actor(), _write())
    service.remember(
        _actor(),
        _write(
            value="en-US",
            observed_at=NOW + timedelta(days=1),
            expires_at=NOW + timedelta(days=31),
            idempotency_key="language-v2",
        ),
    )
    command = MemoryDeleteCommand(
        subject_id="customer-001",
        memory_key="preference.language",
        reason="Customer requested memory deletion.",
        idempotency_key="delete-language",
        request_id="request-delete-language",
        trace_id="trace-delete-language",
    )

    first = service.forget(_actor(Role.SUPERVISOR), command)
    second = service.forget(_actor(Role.SUPERVISOR), command)

    assert first == second
    assert first.decision is MemoryDecision.DELETED
    assert all(item.status is MemoryStatus.DELETED for item in repository.memories)
    assert all(item.value is None for item in repository.memories)
    assert all(len(item.value_hash) == 64 for item in repository.memories)


def test_customer_scope_and_agent_delete_permissions_are_enforced() -> None:
    service, _, audit = _service()
    customer = _actor(Role.CUSTOMER, actor_id="customer-001")
    with pytest.raises(AuthorizationDeniedError):
        service.remember(
            customer,
            _write(subject_id="customer-002"),
        )
    with pytest.raises(AuthorizationDeniedError):
        service.forget(
            _actor(Role.AGENT),
            MemoryDeleteCommand(
                subject_id="customer-001",
                memory_key="preference.language",
                reason="Attempted delete.",
                idempotency_key="agent-delete",
                request_id="request-agent-delete",
                trace_id="trace-agent-delete",
            ),
        )
    events = audit.list_for_tenant(TENANT_ID, limit=10)
    assert len(events) == 2
    assert all(event.decision.value == "deny" for event in events)


def test_idempotency_key_replay_and_conflict() -> None:
    service, repository, _ = _service()
    command = _write()

    first = service.remember(_actor(), command)
    second = service.remember(_actor(), command)

    assert first == second
    assert len(repository.memories) == 1
    assert len(repository.events) == 1
    with pytest.raises(MemoryIdempotencyConflictError):
        service.remember(
            _actor(),
            command.model_copy(update={"value": "en-US"}),
        )


def test_tenant_scope_isolated_even_for_same_subject_and_key() -> None:
    repository = InMemoryMemoryRepository()
    audit = InMemoryAuthorizationAuditRepository()
    service = MemoryService(
        repository,
        AuthorizationService(audit),
        clock=lambda: NOW,
    )
    service.remember(_actor(), _write())
    service.remember(
        _actor(tenant_id=OTHER_TENANT_ID),
        _write(value="en-US"),
    )

    assert [
        item.value
        for item in repository.list_active_for_subject(
            TENANT_ID,
            "customer-001",
            at=NOW,
        )
    ] == ["zh-cn"]
    assert [
        item.value
        for item in repository.list_active_for_subject(
            OTHER_TENANT_ID,
            "customer-001",
            at=NOW,
        )
    ] == ["en-us"]
