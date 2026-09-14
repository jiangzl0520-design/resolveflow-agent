from datetime import UTC, datetime
from uuid import UUID

from app.domain.auth import AuthenticatedActor, Role
from app.domain.investigation_triage import EvidenceKind
from app.domain.ticket import TicketCategory
from app.tools.after_sales import (
    InMemoryAfterSalesDataSource,
    LogisticsObservation,
    LogisticsStatus,
    OrderObservation,
    OrderStatus,
    PolicyObservation,
    SimulatedLogistics,
    SimulatedOrder,
    SimulatedPolicy,
    build_after_sales_tools,
)
from app.tools.contracts import (
    ToolCallRequest,
    ToolExecutionContext,
    ToolFailureKind,
    ToolSideEffect,
)
from app.tools.executor import (
    InMemoryToolCallRecorder,
    ToolExecutor,
)
from app.tools.registry import ToolRegistry

TENANT_ID = UUID("70000000-0000-0000-0000-000000000001")
OTHER_TENANT_ID = UUID(
    "70000000-0000-0000-0000-000000000002"
)
OBSERVED_AT = datetime(2026, 7, 25, 9, 0, tzinfo=UTC)


def data_source() -> InMemoryAfterSalesDataSource:
    return InMemoryAfterSalesDataSource(
        orders=[
            SimulatedOrder(
                tenant_id=TENANT_ID,
                order_id="10086",
                status=OrderStatus.SHIPPED,
                amount_minor=12900,
                currency="CNY",
                observed_at=OBSERVED_AT,
            ),
            SimulatedOrder(
                tenant_id=OTHER_TENANT_ID,
                order_id="10086",
                status=OrderStatus.CANCELLED,
                amount_minor=8800,
                currency="CNY",
                observed_at=OBSERVED_AT,
            ),
        ],
        logistics=[
            SimulatedLogistics(
                tenant_id=TENANT_ID,
                order_id="10086",
                status=LogisticsStatus.DELIVERED,
                proof_available=True,
                signed_at=OBSERVED_AT,
                observed_at=OBSERVED_AT,
            )
        ],
        policies=[
            SimulatedPolicy(
                policy_id="CN-NOT-RECEIVED-001",
                version="2026.07",
                category=TicketCategory.NOT_RECEIVED,
                region="CN",
                required_evidence=(
                    EvidenceKind.ORDER_STATUS,
                    EvidenceKind.LOGISTICS_TRACE,
                    EvidenceKind.DELIVERY_PROOF,
                    EvidenceKind.APPLICABLE_POLICY,
                ),
                requires_human_approval=True,
                summary=(
                    "物流显示签收但用户否认收货时，必须核验签收证明。"
                ),
                effective_at=OBSERVED_AT,
            )
        ],
    )


def execution_context(
    *,
    tenant_id: UUID = TENANT_ID,
    role: Role = Role.AGENT,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        actor=AuthenticatedActor(
            actor_id="agent-tool-001",
            tenant_id=tenant_id,
            roles=frozenset({role}),
        ),
        request_id="request-after-sales",
        trace_id="trace-after-sales",
        agent_run_id="run-after-sales",
        agent_step_id="step-after-sales",
    )


def call(
    name: str,
    arguments: dict,
    *,
    context=None,
) -> ToolCallRequest:
    return ToolCallRequest(
        tool_name=name,
        tool_version="1.0.0",
        arguments=arguments,
        context=context or execution_context(),
    )


def runtime(
    source: InMemoryAfterSalesDataSource,
    *,
    timeout_seconds: float = 1,
):
    recorder = InMemoryToolCallRecorder()
    executor = ToolExecutor(
        ToolRegistry(
            build_after_sales_tools(
                source,
                timeout_seconds=timeout_seconds,
            )
        ),
        recorder,
    )
    return executor, recorder


def test_three_read_only_tools_return_typed_observations() -> None:
    source = data_source()
    executor, recorder = runtime(source)
    try:
        order = executor.execute(call("order_lookup", {"order_id": "10086"}))
        logistics = executor.execute(
            call("logistics_lookup", {"order_id": "10086"})
        )
        policy = executor.execute(
            call(
                "policy_lookup",
                {"category": "not_received", "region": "CN"},
            )
        )
    finally:
        executor.close()

    assert isinstance(order.output, OrderObservation)
    assert order.output.status is OrderStatus.SHIPPED
    assert order.output.paid is True
    assert isinstance(logistics.output, LogisticsObservation)
    assert logistics.output.status is LogisticsStatus.DELIVERED
    assert logistics.output.proof_available is True
    assert isinstance(policy.output, PolicyObservation)
    assert EvidenceKind.DELIVERY_PROOF in policy.output.required_evidence
    assert policy.output.requires_human_approval is True
    assert all(
        item.side_effect is ToolSideEffect.READ_ONLY
        for item in recorder.observations
    )


def test_tenant_is_injected_and_cross_tenant_order_is_hidden() -> None:
    source = data_source()
    executor, _ = runtime(source)
    try:
        tenant_a = executor.execute(
            call("order_lookup", {"order_id": "10086"})
        )
        unknown_tenant = executor.execute(
            call(
                "logistics_lookup",
                {"order_id": "10086"},
                context=execution_context(tenant_id=OTHER_TENANT_ID),
            )
        )
    finally:
        executor.close()

    assert isinstance(tenant_a.output, OrderObservation)
    assert tenant_a.output.status is OrderStatus.SHIPPED
    assert unknown_tenant.error is not None
    assert unknown_tenant.error.kind is ToolFailureKind.RESOURCE
    assert source.calls[0][1] == TENANT_ID
    assert source.calls[1][1] == OTHER_TENANT_ID


def test_model_cannot_override_tenant_in_arguments() -> None:
    source = data_source()
    executor, _ = runtime(source)
    try:
        observation = executor.execute(
            call(
                "order_lookup",
                {
                    "order_id": "10086",
                    "tenant_id": str(OTHER_TENANT_ID),
                },
            )
        )
    finally:
        executor.close()

    assert observation.error is not None
    assert observation.error.kind is ToolFailureKind.ARGUMENT
    assert source.calls == []


def test_dependency_failure_is_retryable_and_distinct() -> None:
    source = data_source()
    source.fail_next("logistics_lookup")
    executor, _ = runtime(source)
    try:
        observation = executor.execute(
            call("logistics_lookup", {"order_id": "10086"})
        )
    finally:
        executor.close()

    assert observation.error is not None
    assert observation.error.kind is ToolFailureKind.DEPENDENCY
    assert observation.error.code == "tool_dependency_unavailable"
    assert observation.error.retryable is True


def test_read_only_timeout_is_standardized() -> None:
    source = data_source()
    source.set_delay("order_lookup", 0.03)
    executor, _ = runtime(source, timeout_seconds=0.005)
    try:
        observation = executor.execute(
            call("order_lookup", {"order_id": "10086"})
        )
    finally:
        executor.close()

    assert observation.error is not None
    assert observation.error.kind is ToolFailureKind.TIMEOUT
    assert observation.error.code == "tool_timeout"
    assert observation.error.retryable is True


def test_customer_cannot_discover_or_execute_internal_tools() -> None:
    source = data_source()
    registry = ToolRegistry(build_after_sales_tools(source))
    customer = execution_context(role=Role.CUSTOMER)
    executor = ToolExecutor(registry, InMemoryToolCallRecorder())
    try:
        observation = executor.execute(
            call(
                "order_lookup",
                {"order_id": "10086"},
                context=customer,
            )
        )
    finally:
        executor.close()

    assert registry.descriptors_for(customer.actor) == ()
    assert observation.error is not None
    assert observation.error.kind is ToolFailureKind.AUTHORIZATION
    assert source.calls == []


def test_argument_schema_distinguishes_bad_parameter_from_missing_resource() -> None:
    source = data_source()
    executor, _ = runtime(source)
    try:
        invalid = executor.execute(
            call("order_lookup", {"order_id": "../secret"})
        )
        missing = executor.execute(
            call("order_lookup", {"order_id": "99999"})
        )
    finally:
        executor.close()

    assert invalid.error is not None
    assert invalid.error.kind is ToolFailureKind.ARGUMENT
    assert missing.error is not None
    assert missing.error.kind is ToolFailureKind.RESOURCE
