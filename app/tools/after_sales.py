from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from threading import RLock
from time import sleep
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.auth import Permission
from app.domain.investigation_triage import EvidenceKind
from app.domain.ticket import TicketCategory
from app.tools.contracts import (
    ToolDefinition,
    ToolExecutionContext,
    ToolRiskLevel,
    ToolSideEffect,
)
from app.tools.errors import (
    ToolDependencyUnavailableError,
    ToolResourceNotFoundError,
)

TOOL_VERSION = "1.0.0"


class OrderStatus(StrEnum):
    PENDING_PAYMENT = "pending_payment"
    PAID = "paid"
    SHIPPED = "shipped"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class LogisticsStatus(StrEnum):
    CREATED = "created"
    IN_TRANSIT = "in_transit"
    DELIVERED = "delivered"
    EXCEPTION = "exception"


class OrderLookupArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(
        min_length=3,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


class OrderObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    order_id: str
    status: OrderStatus
    paid: bool
    shipped: bool
    amount_minor: int = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    observed_at: datetime


class LogisticsLookupArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(
        min_length=3,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


class LogisticsObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    order_id: str
    status: LogisticsStatus
    proof_available: bool
    signed_at: datetime | None
    observed_at: datetime


class PolicyLookupArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: TicketCategory
    region: str = Field(
        default="CN",
        pattern=r"^[A-Z]{2}$",
    )


class PolicyObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_id: str
    version: str
    category: TicketCategory
    region: str
    required_evidence: list[EvidenceKind]
    requires_human_approval: bool
    summary: str = Field(min_length=5, max_length=300)
    effective_at: datetime

    @field_validator("required_evidence")
    @classmethod
    def evidence_must_be_unique(
        cls,
        value: list[EvidenceKind],
    ) -> list[EvidenceKind]:
        if len(value) != len(set(value)):
            raise ValueError("required_evidence must not contain duplicates")
        return value


@dataclass(frozen=True, slots=True)
class SimulatedOrder:
    tenant_id: UUID
    order_id: str
    status: OrderStatus
    amount_minor: int
    currency: str
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class SimulatedLogistics:
    tenant_id: UUID
    order_id: str
    status: LogisticsStatus
    proof_available: bool
    signed_at: datetime | None
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class SimulatedPolicy:
    policy_id: str
    version: str
    category: TicketCategory
    region: str
    required_evidence: tuple[EvidenceKind, ...]
    requires_human_approval: bool
    summary: str
    effective_at: datetime


class InMemoryAfterSalesDataSource:
    def __init__(
        self,
        *,
        orders: list[SimulatedOrder] | None = None,
        logistics: list[SimulatedLogistics] | None = None,
        policies: list[SimulatedPolicy] | None = None,
    ) -> None:
        self._orders = {
            (item.tenant_id, item.order_id): item
            for item in (orders or [])
        }
        self._logistics = {
            (item.tenant_id, item.order_id): item
            for item in (logistics or [])
        }
        self._policies = {
            (item.category, item.region): item
            for item in (policies or [])
        }
        self._failures: dict[str, int] = {}
        self._delays: dict[str, float] = {}
        self._calls: list[tuple[str, UUID, dict[str, Any]]] = []
        self._lock = RLock()

    @property
    def calls(self) -> list[tuple[str, UUID, dict[str, Any]]]:
        with self._lock:
            return list(self._calls)

    def fail_next(self, operation: str, *, count: int = 1) -> None:
        with self._lock:
            self._failures[operation] = count

    def set_delay(self, operation: str, seconds: float) -> None:
        with self._lock:
            self._delays[operation] = seconds

    def get_order(
        self,
        tenant_id: UUID,
        order_id: str,
    ) -> SimulatedOrder:
        self._before_call(
            "order_lookup",
            tenant_id,
            {"order_id": order_id},
        )
        try:
            return self._orders[(tenant_id, order_id)]
        except KeyError as exc:
            raise ToolResourceNotFoundError() from exc

    def get_logistics(
        self,
        tenant_id: UUID,
        order_id: str,
    ) -> SimulatedLogistics:
        self._before_call(
            "logistics_lookup",
            tenant_id,
            {"order_id": order_id},
        )
        try:
            return self._logistics[(tenant_id, order_id)]
        except KeyError as exc:
            raise ToolResourceNotFoundError() from exc

    def get_policy(
        self,
        category: TicketCategory,
        region: str,
        *,
        tenant_id: UUID,
    ) -> SimulatedPolicy:
        self._before_call(
            "policy_lookup",
            tenant_id,
            {"category": category.value, "region": region},
        )
        try:
            return self._policies[(category, region)]
        except KeyError as exc:
            raise ToolResourceNotFoundError() from exc

    def _before_call(
        self,
        operation: str,
        tenant_id: UUID,
        arguments: dict[str, Any],
    ) -> None:
        with self._lock:
            self._calls.append((operation, tenant_id, arguments))
            failure_count = self._failures.get(operation, 0)
            if failure_count:
                self._failures[operation] = failure_count - 1
            delay = self._delays.get(operation, 0)
        if delay:
            sleep(delay)
        if failure_count:
            raise ToolDependencyUnavailableError()


def build_after_sales_tools(
    source: InMemoryAfterSalesDataSource,
    *,
    timeout_seconds: float = 2.0,
) -> list[ToolDefinition]:
    def order_lookup(
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> OrderObservation:
        parsed = OrderLookupArguments.model_validate(arguments)
        order = source.get_order(
            context.tenant_id,
            parsed.order_id,
        )
        return OrderObservation(
            order_id=order.order_id,
            status=order.status,
            paid=order.status
            in {
                OrderStatus.PAID,
                OrderStatus.SHIPPED,
                OrderStatus.REFUNDED,
            },
            shipped=order.status
            in {OrderStatus.SHIPPED, OrderStatus.REFUNDED},
            amount_minor=order.amount_minor,
            currency=order.currency,
            observed_at=order.observed_at,
        )

    def logistics_lookup(
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> LogisticsObservation:
        parsed = LogisticsLookupArguments.model_validate(arguments)
        item = source.get_logistics(
            context.tenant_id,
            parsed.order_id,
        )
        return LogisticsObservation(
            order_id=item.order_id,
            status=item.status,
            proof_available=item.proof_available,
            signed_at=item.signed_at,
            observed_at=item.observed_at,
        )

    def policy_lookup(
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> PolicyObservation:
        parsed = PolicyLookupArguments.model_validate(arguments)
        item = source.get_policy(
            parsed.category,
            parsed.region,
            tenant_id=context.tenant_id,
        )
        return PolicyObservation(
            policy_id=item.policy_id,
            version=item.version,
            category=item.category,
            region=item.region,
            required_evidence=list(item.required_evidence),
            requires_human_approval=item.requires_human_approval,
            summary=item.summary,
            effective_at=item.effective_at,
        )

    common = {
        "version": TOOL_VERSION,
        "risk_level": ToolRiskLevel.LOW,
        "side_effect": ToolSideEffect.READ_ONLY,
        "timeout_seconds": timeout_seconds,
    }
    return [
        ToolDefinition(
            name="order_lookup",
            description=(
                "Read the tenant-scoped payment and fulfillment state "
                "for one exact order identifier."
            ),
            input_model=OrderLookupArguments,
            output_model=OrderObservation,
            required_permission=Permission.TOOL_ORDER_READ,
            handler=order_lookup,
            **common,
        ),
        ToolDefinition(
            name="logistics_lookup",
            description=(
                "Read the latest tenant-scoped delivery status and "
                "whether delivery proof exists for one exact order."
            ),
            input_model=LogisticsLookupArguments,
            output_model=LogisticsObservation,
            required_permission=Permission.TOOL_LOGISTICS_READ,
            handler=logistics_lookup,
            **common,
        ),
        ToolDefinition(
            name="policy_lookup",
            description=(
                "Read the effective evidence and approval requirements "
                "for one after-sales category and region."
            ),
            input_model=PolicyLookupArguments,
            output_model=PolicyObservation,
            required_permission=Permission.TOOL_POLICY_READ,
            handler=policy_lookup,
            **common,
        ),
    ]
