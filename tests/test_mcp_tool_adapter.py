from datetime import UTC, datetime
from uuid import UUID

import pytest
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, Tool, ToolAnnotations

from app.core.config import AppEnvironment, Settings
from app.domain.auth import AuthenticatedActor, Role
from app.mcp.client import CircuitBreaker, MCPOrderToolAdapter
from app.mcp.contracts import (
    MCPToolContractError,
    ORDER_TOOL_NAME,
    order_tool_metadata,
)
from app.mcp.security import MCPServiceTokenIssuer
from app.tools.after_sales import (
    OrderLookupArguments,
    OrderObservation,
    OrderStatus,
)
from app.tools.contracts import (
    ToolCallRequest,
    ToolExecutionContext,
    ToolFailureKind,
)
from app.tools.executor import InMemoryToolCallRecorder, ToolExecutor
from app.tools.errors import ToolCircuitOpenError
from app.tools.registry import ToolRegistry

TENANT_ID = UUID("72000000-0000-0000-0000-000000000001")
OBSERVED_AT = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)


class FakeGateway:
    def __init__(self, tool: Tool | None = None) -> None:
        self.tool = tool or valid_remote_tool()
        self.results: list[CallToolResult | Exception] = []
        self.call_count = 0
        self.tokens: list[str] = []

    def list_tools(self, token: str) -> list[Tool]:
        self.tokens.append(token)
        return [self.tool]

    def call_tool(
        self,
        token: str,
        *,
        name: str,
        arguments: dict,
    ) -> CallToolResult:
        self.call_count += 1
        self.tokens.append(token)
        assert name == ORDER_TOOL_NAME
        assert arguments == {"order_id": "10086"}
        next_result = self.results.pop(0)
        if isinstance(next_result, Exception):
            raise next_result
        return next_result


def settings() -> Settings:
    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        environment=AppEnvironment.TEST,
        mcp_jwt_secret=(
            "day11-adapter-test-secret-that-is-longer-than-thirty-two"
        ),
        mcp_order_server_url="http://127.0.0.1:18011/mcp",
    )


def context() -> ToolExecutionContext:
    return ToolExecutionContext(
        actor=AuthenticatedActor(
            actor_id="agent-adapter-001",
            tenant_id=TENANT_ID,
            roles=frozenset({Role.AGENT}),
        ),
        request_id="request-adapter",
        trace_id="trace-adapter",
        agent_run_id="run-adapter",
        agent_step_id="step-adapter",
    )


def valid_remote_tool() -> Tool:
    schema = OrderLookupArguments.model_json_schema()
    return Tool(
        name=ORDER_TOOL_NAME,
        description="Read an order.",
        inputSchema=schema,
        outputSchema=OrderObservation.model_json_schema(),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        _meta=order_tool_metadata(),
    )


def successful_result() -> CallToolResult:
    return CallToolResult(
        content=[],
        structuredContent={
            "order_id": "10086",
            "status": "shipped",
            "paid": True,
            "shipped": True,
            "amount_minor": 12900,
            "currency": "CNY",
            "observed_at": OBSERVED_AT.isoformat(),
        },
    )


def build_adapter(
    gateway: FakeGateway,
    *,
    breaker: CircuitBreaker | None = None,
) -> MCPOrderToolAdapter:
    return MCPOrderToolAdapter(
        gateway,
        MCPServiceTokenIssuer(settings()),
        timeout_seconds=1,
        circuit_breaker=breaker
        or CircuitBreaker(
            failure_threshold=3,
            recovery_seconds=10,
        ),
    )


def execute(
    adapter: MCPOrderToolAdapter,
) -> tuple:
    definition = adapter.discover(context())
    recorder = InMemoryToolCallRecorder()
    executor = ToolExecutor(ToolRegistry([definition]), recorder)
    try:
        observation = executor.execute(
            ToolCallRequest(
                tool_name=ORDER_TOOL_NAME,
                tool_version="1.0.0",
                arguments={"order_id": "10086"},
                context=context(),
            )
        )
    finally:
        executor.close()
    return observation, recorder


def test_discovered_mcp_tool_preserves_local_tool_contract() -> None:
    gateway = FakeGateway()
    gateway.results.append(successful_result())

    observation, recorder = execute(build_adapter(gateway))

    assert isinstance(observation.output, OrderObservation)
    assert observation.output.status is OrderStatus.SHIPPED
    assert observation.input_schema_hash is not None
    assert observation.output_schema_hash is not None
    assert recorder.observations == [observation]
    assert len(gateway.tokens) == 2


def test_contract_drift_is_rejected_before_agent_exposure() -> None:
    drifted = valid_remote_tool()
    drifted.meta["resolveflow/toolVersion"] = "2.0.0"
    gateway = FakeGateway(drifted)

    with pytest.raises(MCPToolContractError):
        build_adapter(gateway).discover(context())

    assert gateway.call_count == 0


def test_invalid_remote_output_is_not_accepted_as_observation() -> None:
    gateway = FakeGateway()
    gateway.results.append(
        CallToolResult(
            content=[],
            structuredContent={
                "order_id": "10086",
                "status": "shipped",
            },
        )
    )

    observation, _ = execute(build_adapter(gateway))

    assert observation.error is not None
    assert observation.error.kind is ToolFailureKind.OUTPUT_CONTRACT
    assert observation.error.code == "tool_remote_contract_invalid"


def test_circuit_opens_and_recovers_with_one_half_open_probe() -> None:
    now = [100.0]
    breaker = CircuitBreaker(
        failure_threshold=2,
        recovery_seconds=5,
        clock=lambda: now[0],
    )
    gateway = FakeGateway()
    dependency_error = MCPError(
        -32042,
        "dependency unavailable",
        {
            "resolveflow_code": "tool_dependency_unavailable",
            "retryable": True,
        },
    )
    gateway.results.extend(
        [dependency_error, dependency_error, successful_result()]
    )
    adapter = build_adapter(gateway, breaker=breaker)

    first, _ = execute(adapter)
    second, _ = execute(adapter)
    blocked, _ = execute(adapter)

    assert first.error is not None
    assert first.error.kind is ToolFailureKind.DEPENDENCY
    assert second.error is not None
    assert second.error.kind is ToolFailureKind.DEPENDENCY
    assert blocked.error is not None
    assert blocked.error.code == "tool_circuit_open"
    assert gateway.call_count == 2
    assert breaker.snapshot.state == "open"

    now[0] += 5
    recovered, _ = execute(adapter)

    assert recovered.succeeded
    assert gateway.call_count == 3
    assert breaker.snapshot.state == "closed"


def test_half_open_allows_only_one_probe_until_result_is_known() -> None:
    now = [100.0]
    breaker = CircuitBreaker(
        failure_threshold=1,
        recovery_seconds=5,
        clock=lambda: now[0],
    )
    breaker.record_dependency_failure()
    now[0] += 5

    breaker.before_call()

    with pytest.raises(ToolCircuitOpenError):
        breaker.before_call()

    breaker.record_success()
    breaker.before_call()
