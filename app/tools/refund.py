from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from hmac import compare_digest, new as new_hmac
import json
from threading import RLock
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.domain.auth import Permission
from app.tools.contracts import (
    ToolDefinition,
    ToolExecutionContext,
    ToolRiskLevel,
    ToolSideEffect,
)
from app.tools.errors import (
    ToolExecutionGrantError,
    ToolIdempotencyConflictError,
    ToolResourceNotFoundError,
)

REFUND_TOOL_VERSION = "1.0.0"


class RefundExecutionStatus(StrEnum):
    PROCESSED = "processed"


class RefundExecuteArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(min_length=3, max_length=64)
    amount_minor: int = Field(gt=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    reason: str = Field(min_length=5, max_length=300)
    proposal_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    approval_id: UUID
    idempotency_key: str = Field(min_length=16, max_length=96)
    execution_grant: str = Field(pattern=r"^[a-f0-9]{64}$")


class RefundStatusArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=16, max_length=96)


class RefundObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    refund_id: UUID
    order_id: str
    amount_minor: int
    currency: str
    proposal_hash: str
    approval_id: UUID
    idempotency_key: str
    status: RefundExecutionStatus
    processed_at: datetime


@dataclass(frozen=True, slots=True)
class SimulatedRefund:
    tenant_id: UUID
    fingerprint: str
    observation: RefundObservation


class InMemoryRefundDataSource:
    """Simulates an external payment system with tenant-scoped idempotency."""

    def __init__(self) -> None:
        self._refunds: dict[tuple[UUID, str], SimulatedRefund] = {}
        self._calls: list[tuple[str, UUID, dict[str, Any]]] = []
        self._lock = RLock()

    @property
    def calls(self) -> list[tuple[str, UUID, dict[str, Any]]]:
        with self._lock:
            return list(self._calls)

    @property
    def execution_count(self) -> int:
        with self._lock:
            return len(self._refunds)

    def execute(
        self,
        tenant_id: UUID,
        arguments: RefundExecuteArguments,
        *,
        now: datetime | None = None,
    ) -> RefundObservation:
        document = arguments.model_dump(mode="json")
        fingerprint = _document_hash(document)
        key = (tenant_id, arguments.idempotency_key)
        with self._lock:
            self._calls.append(
                (
                    "refund_execute",
                    tenant_id,
                    {
                        **document,
                        "execution_grant": "[REDACTED]",
                    },
                )
            )
            existing = self._refunds.get(key)
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise ToolIdempotencyConflictError()
                return existing.observation
            observation = RefundObservation(
                refund_id=uuid4(),
                order_id=arguments.order_id,
                amount_minor=arguments.amount_minor,
                currency=arguments.currency,
                proposal_hash=arguments.proposal_hash,
                approval_id=arguments.approval_id,
                idempotency_key=arguments.idempotency_key,
                status=RefundExecutionStatus.PROCESSED,
                processed_at=now or datetime.now(UTC),
            )
            self._refunds[key] = SimulatedRefund(
                tenant_id=tenant_id,
                fingerprint=fingerprint,
                observation=observation,
            )
            return observation

    def get_status(
        self,
        tenant_id: UUID,
        idempotency_key: str,
    ) -> RefundObservation:
        with self._lock:
            self._calls.append(
                (
                    "refund_status_lookup",
                    tenant_id,
                    {"idempotency_key": idempotency_key},
                )
            )
            try:
                return self._refunds[(tenant_id, idempotency_key)].observation
            except KeyError as exc:
                raise ToolResourceNotFoundError() from exc


class RefundExecutionGuard:
    """Issues and verifies exact-operation HMAC capabilities."""

    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError(
                "Refund execution guard secret must be at least 32 bytes."
            )
        self._secret = secret

    def issue(
        self,
        arguments: dict[str, Any],
        *,
        tenant_id: UUID,
        actor_id: str,
    ) -> str:
        return self._signature(
            arguments,
            tenant_id=tenant_id,
            actor_id=actor_id,
        )

    def verify(
        self,
        arguments: RefundExecuteArguments,
        *,
        tenant_id: UUID,
        actor_id: str,
    ) -> bool:
        document = arguments.model_dump(
            mode="json",
            exclude={"execution_grant"},
        )
        expected = self._signature(
            document,
            tenant_id=tenant_id,
            actor_id=actor_id,
        )
        return compare_digest(expected, arguments.execution_grant)

    def _signature(
        self,
        arguments: dict[str, Any],
        *,
        tenant_id: UUID,
        actor_id: str,
    ) -> str:
        payload = {
            "tenant_id": str(tenant_id),
            "actor_id": actor_id,
            "arguments": arguments,
        }
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return new_hmac(
            self._secret,
            serialized,
            sha256,
        ).hexdigest()


def build_refund_tools(
    source: InMemoryRefundDataSource,
    execution_guard: RefundExecutionGuard,
    *,
    timeout_seconds: float = 2.0,
) -> list[ToolDefinition]:
    def execute_refund(
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> RefundObservation:
        parsed = RefundExecuteArguments.model_validate(arguments)
        if not execution_guard.verify(
            parsed,
            tenant_id=context.tenant_id,
            actor_id=context.actor.actor_id,
        ):
            raise ToolExecutionGrantError()
        return source.execute(context.tenant_id, parsed)

    def lookup_refund(
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> RefundObservation:
        parsed = RefundStatusArguments.model_validate(arguments)
        return source.get_status(
            context.tenant_id,
            parsed.idempotency_key,
        )

    return [
        ToolDefinition(
            name="refund_execute",
            version=REFUND_TOOL_VERSION,
            description=(
                "Execute one already policy-approved refund. This critical "
                "write tool accepts only backend-bound proposal parameters."
            ),
            input_model=RefundExecuteArguments,
            output_model=RefundObservation,
            risk_level=ToolRiskLevel.CRITICAL,
            side_effect=ToolSideEffect.WRITE,
            required_permission=Permission.TOOL_REFUND_EXECUTE,
            timeout_seconds=timeout_seconds,
            handler=execute_refund,
        ),
        ToolDefinition(
            name="refund_status_lookup",
            version=REFUND_TOOL_VERSION,
            description=(
                "Read refund execution state by the backend-generated "
                "idempotency key."
            ),
            input_model=RefundStatusArguments,
            output_model=RefundObservation,
            risk_level=ToolRiskLevel.LOW,
            side_effect=ToolSideEffect.READ_ONLY,
            required_permission=Permission.TOOL_REFUND_STATUS_READ,
            timeout_seconds=timeout_seconds,
            handler=lookup_refund,
        ),
    ]


def _document_hash(document: dict) -> str:
    serialized = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(serialized).hexdigest()
