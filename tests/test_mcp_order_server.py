import asyncio
from datetime import UTC, datetime
from uuid import UUID

from mcp.client import Client
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

from app.core.config import AppEnvironment, Settings
from app.domain.auth import AuthenticatedActor, Permission, Role
from app.mcp.contracts import InMemoryMCPAuditSink
from app.mcp.order_server import build_order_mcp_server
from app.mcp.security import (
    MCPServiceTokenIssuer,
    MCPServiceTokenVerifier,
)
from app.tools.after_sales import (
    InMemoryAfterSalesDataSource,
    OrderObservation,
    OrderStatus,
    SimulatedOrder,
)
from app.tools.contracts import ToolExecutionContext

TENANT_ID = UUID("74000000-0000-0000-0000-000000000001")
OBSERVED_AT = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)


def test_mcp_server_returns_structured_output_and_audits_trace() -> None:
    runtime_settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        environment=AppEnvironment.TEST,
        mcp_jwt_secret=(
            "day11-server-test-secret-that-is-longer-than-thirty-two"
        ),
        mcp_order_server_url="http://127.0.0.1:18011/mcp",
    )
    source = InMemoryAfterSalesDataSource(
        orders=[
            SimulatedOrder(
                tenant_id=TENANT_ID,
                order_id="10086",
                status=OrderStatus.SHIPPED,
                amount_minor=12900,
                currency="CNY",
                observed_at=OBSERVED_AT,
            )
        ]
    )
    audit = InMemoryMCPAuditSink()
    server = build_order_mcp_server(
        source,
        runtime_settings,
        audit_sink=audit,
    )
    execution_context = ToolExecutionContext(
        actor=AuthenticatedActor(
            actor_id="agent-server-001",
            tenant_id=TENANT_ID,
            roles=frozenset({Role.AGENT}),
        ),
        request_id="request-server",
        trace_id="trace-server",
        agent_run_id="run-server",
        agent_step_id="step-server",
    )

    async def scenario():
        raw_token = MCPServiceTokenIssuer(runtime_settings).issue(
            execution_context,
            permissions=frozenset({Permission.TOOL_ORDER_READ}),
        )
        access_token = await MCPServiceTokenVerifier(
            runtime_settings
        ).verify_token(raw_token)
        assert access_token is not None
        context_token = auth_context_var.set(
            AuthenticatedUser(access_token)
        )
        try:
            async with Client(server) as client:
                tools = await client.list_tools()
                result = await client.call_tool(
                    "order_lookup",
                    {"order_id": "10086"},
                )
                return tools, result
        finally:
            auth_context_var.reset(context_token)

    tools, result = asyncio.run(scenario())

    assert [tool.name for tool in tools.tools] == ["order_lookup"]
    assert result.is_error is False
    output = OrderObservation.model_validate(result.structured_content)
    assert output.status is OrderStatus.SHIPPED
    assert len(audit.records) == 1
    assert audit.records[0].tenant_id == TENANT_ID
    assert audit.records[0].actor_id == "agent-server-001"
    assert audit.records[0].request_id == "request-server"
    assert audit.records[0].trace_id == "trace-server"
    assert audit.records[0].outcome == "succeeded"

