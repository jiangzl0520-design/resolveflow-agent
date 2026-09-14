from datetime import UTC, datetime
from hashlib import sha256
import json
from typing import Callable
from uuid import uuid4

from app.domain.auth import AuthenticatedActor, Permission
from app.domain.memory import (
    LongTermMemory,
    MemoryDecision,
    MemoryDeleteCommand,
    MemoryMutation,
    MemoryOperationResult,
    MemorySourceType,
    MemoryStatus,
    MemoryTransaction,
    MemoryWriteCommand,
)
from app.memory.policy import MEMORY_KEY_POLICIES, normalize_memory_value
from app.memory.errors import MemoryPolicyValidationError
from app.repositories.memory_repository import MemoryRepository
from app.services.authorization_service import AuthorizationService


class MemoryService:
    def __init__(
        self,
        repository: MemoryRepository,
        authorization: AuthorizationService,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._authorization = authorization
        self._clock = clock

    def remember(
        self,
        actor: AuthenticatedActor,
        command: MemoryWriteCommand,
    ) -> MemoryOperationResult:
        self._authorization.require_subject_memory_access(
            actor,
            Permission.MEMORY_WRITE,
            subject_id=command.subject_id,
            request_id=command.request_id,
        )
        try:
            normalized = normalize_memory_value(
                command.memory_key,
                command.value,
            )
        except ValueError as exc:
            raise MemoryPolicyValidationError(str(exc)) from exc
        now = self._clock()
        request_document = command.model_dump(
            mode="json",
            exclude={"request_id", "trace_id"},
        )
        request_document["value"] = normalized
        transaction = MemoryTransaction(
            tenant_id=actor.tenant_id,
            subject_id=command.subject_id,
            memory_key=command.memory_key,
            actor_id=actor.actor_id,
            request_hash=_hash_json(request_document),
            candidate_hash=_hash_text(normalized),
            idempotency_key=command.idempotency_key,
            request_id=command.request_id,
            trace_id=command.trace_id,
            created_at=now,
        )
        policy = MEMORY_KEY_POLICIES.get(command.memory_key)

        def resolve(records: tuple[LongTermMemory, ...]) -> MemoryMutation:
            if policy is None:
                return _no_write(command.memory_key, MemoryDecision.REJECTED)
            if command.source_type is MemorySourceType.MODEL_INFERENCE:
                return _no_write(
                    command.memory_key,
                    MemoryDecision.CONFIRMATION_REQUIRED,
                )
            if (
                command.source_type not in policy.allowed_sources
                or command.confidence < policy.minimum_confidence
                or command.expires_at - command.observed_at
                > policy.maximum_ttl
            ):
                return _no_write(command.memory_key, MemoryDecision.REJECTED)

            expired = tuple(
                memory.model_copy(
                    update={
                        "status": MemoryStatus.EXPIRED,
                        "updated_at": now,
                    }
                )
                for memory in records
                if memory.status in {
                    MemoryStatus.ACTIVE,
                    MemoryStatus.CONFLICTED,
                }
                and memory.expires_at <= now
            )
            expired_ids = {memory.id for memory in expired}
            live = tuple(
                memory
                for memory in records
                if memory.status in {
                    MemoryStatus.ACTIVE,
                    MemoryStatus.CONFLICTED,
                }
                and memory.id not in expired_ids
            )
            max_version = max((item.version for item in records), default=0)
            active_same = next(
                (
                    item
                    for item in live
                    if item.status is MemoryStatus.ACTIVE
                    and item.value_hash == _hash_text(normalized)
                ),
                None,
            )
            if active_same is not None:
                return MemoryMutation(
                    updated=expired,
                    created=None,
                    result=MemoryOperationResult(
                        decision=MemoryDecision.UNCHANGED,
                        memory_id=active_same.id,
                        memory_key=command.memory_key,
                    ),
                )

            latest_observed = max(
                (item.observed_at for item in live),
                default=None,
            )
            if not live or (
                latest_observed is not None
                and command.observed_at > latest_observed
            ):
                superseded = tuple(
                    item.model_copy(
                        update={
                            "status": MemoryStatus.SUPERSEDED,
                            "updated_at": now,
                        }
                    )
                    for item in live
                )
                created = _new_memory(
                    actor,
                    command,
                    normalized,
                    category=policy.category,
                    status=MemoryStatus.ACTIVE,
                    version=max_version + 1,
                    now=now,
                )
                return MemoryMutation(
                    updated=(*expired, *superseded),
                    created=created,
                    result=MemoryOperationResult(
                        decision=(
                            MemoryDecision.CREATED
                            if not live
                            else MemoryDecision.UPDATED
                        ),
                        memory_id=created.id,
                        memory_key=command.memory_key,
                    ),
                )

            conflicted = tuple(
                item.model_copy(
                    update={
                        "status": MemoryStatus.CONFLICTED,
                        "updated_at": now,
                    }
                )
                for item in live
            )
            created = _new_memory(
                actor,
                command,
                normalized,
                category=policy.category,
                status=MemoryStatus.CONFLICTED,
                version=max_version + 1,
                now=now,
            )
            return MemoryMutation(
                updated=(*expired, *conflicted),
                created=created,
                result=MemoryOperationResult(
                    decision=MemoryDecision.CONFLICTED,
                    memory_id=created.id,
                    memory_key=command.memory_key,
                ),
            )

        return self._repository.transact(transaction, resolve)

    def forget(
        self,
        actor: AuthenticatedActor,
        command: MemoryDeleteCommand,
    ) -> MemoryOperationResult:
        self._authorization.require_subject_memory_access(
            actor,
            Permission.MEMORY_DELETE,
            subject_id=command.subject_id,
            request_id=command.request_id,
        )
        now = self._clock()
        transaction = MemoryTransaction(
            tenant_id=actor.tenant_id,
            subject_id=command.subject_id,
            memory_key=command.memory_key,
            actor_id=actor.actor_id,
            request_hash=_hash_json(
                command.model_dump(
                    mode="json",
                    exclude={"request_id", "trace_id"},
                )
            ),
            candidate_hash=_hash_text(command.reason),
            idempotency_key=command.idempotency_key,
            request_id=command.request_id,
            trace_id=command.trace_id,
            created_at=now,
        )

        def resolve(records: tuple[LongTermMemory, ...]) -> MemoryMutation:
            visible = tuple(
                item
                for item in records
                if item.status is not MemoryStatus.DELETED
            )
            if not visible:
                return _no_write(
                    command.memory_key,
                    MemoryDecision.NOT_FOUND,
                )
            deleted = tuple(
                item.model_copy(
                    update={
                        "status": MemoryStatus.DELETED,
                        "value": None,
                        "updated_at": now,
                    }
                )
                for item in visible
            )
            return MemoryMutation(
                updated=deleted,
                created=None,
                result=MemoryOperationResult(
                    decision=MemoryDecision.DELETED,
                    memory_id=None,
                    memory_key=command.memory_key,
                ),
            )

        return self._repository.transact(transaction, resolve)

    def recall_for_subject(
        self,
        actor: AuthenticatedActor,
        subject_id: str,
        *,
        request_id: str,
    ) -> tuple[LongTermMemory, ...]:
        self._authorization.require_subject_memory_access(
            actor,
            Permission.MEMORY_READ,
            subject_id=subject_id,
            request_id=request_id,
        )
        return self._repository.list_active_for_subject(
            actor.tenant_id,
            subject_id,
            at=self._clock(),
        )


def _new_memory(
    actor: AuthenticatedActor,
    command: MemoryWriteCommand,
    normalized: str,
    *,
    category,
    status: MemoryStatus,
    version: int,
    now: datetime,
) -> LongTermMemory:
    return LongTermMemory(
        id=uuid4(),
        tenant_id=actor.tenant_id,
        subject_id=command.subject_id,
        memory_key=command.memory_key,
        category=category,
        value=normalized,
        value_hash=_hash_text(normalized),
        status=status,
        version=version,
        source_type=command.source_type,
        source_reference_hash=_hash_text(command.source_reference),
        confidence=command.confidence,
        observed_at=command.observed_at,
        expires_at=command.expires_at,
        created_by_actor_id=actor.actor_id,
        created_at=now,
        updated_at=now,
    )


def _no_write(
    memory_key: str,
    decision: MemoryDecision,
) -> MemoryMutation:
    return MemoryMutation(
        updated=(),
        created=None,
        result=MemoryOperationResult(
            decision=decision,
            memory_id=None,
            memory_key=memory_key,
        ),
    )


def _hash_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _hash_json(value) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return _hash_text(serialized)
