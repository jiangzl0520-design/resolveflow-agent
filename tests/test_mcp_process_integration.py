import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from time import monotonic, sleep
from uuid import UUID

import pytest

from app.core.config import AppEnvironment, Settings
from app.core.security import JwtTokenService
from app.domain.auth import AuthenticatedActor, Permission, Role
from app.mcp.client import (
    MCPOrderToolAdapter,
    StreamableHttpMCPGateway,
)
from app.mcp.security import MCPServiceTokenIssuer
from app.tools.after_sales import OrderObservation, OrderStatus
from app.tools.contracts import ToolCallRequest, ToolExecutionContext
from app.tools.executor import InMemoryToolCallRecorder, ToolExecutor
from app.tools.registry import ToolRegistry

TENANT_ID = UUID("73000000-0000-0000-0000-000000000001")
OTHER_TENANT_ID = UUID("73000000-0000-0000-0000-000000000002")
MCP_SECRET = "day11-process-mcp-secret-that-is-longer-than-thirty-two"
APP_SECRET = "day11-process-app-secret-that-is-longer-than-thirty-two"


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_port(port: int, process: subprocess.Popen) -> None:
    deadline = monotonic() + 15
    while monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("MCP test server exited before becoming ready.")
        try:
            with socket.create_connection(
                ("127.0.0.1", port),
                timeout=0.1,
            ):
                return
        except OSError:
            sleep(0.05)
    raise TimeoutError("MCP test server did not become ready.")


@pytest.fixture()
def mcp_process():
    port = _free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    env = os.environ.copy()
    env.update(
        {
            "APP_ENV": "test",
            "MCP_JWT_SECRET": MCP_SECRET,
            "MCP_ORDER_SERVER_URL": url,
            "MCP_ORDER_FIXTURES_JSON": json.dumps(
                [
                    {
                        "tenant_id": str(TENANT_ID),
                        "order_id": "10086",
                        "status": "shipped",
                        "amount_minor": 12900,
                        "currency": "CNY",
                        "observed_at": "2026-07-30T09:00:00Z",
                    }
                ]
            ),
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.mcp.runtime:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=Path(__file__).parents[1],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_port(port, process)
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _settings(url: str) -> Settings:
    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        environment=AppEnvironment.TEST,
        jwt_secret=APP_SECRET,
        mcp_jwt_secret=MCP_SECRET,
        mcp_order_server_url=url,
        mcp_timeout_seconds=2,
    )


def _context(tenant_id: UUID = TENANT_ID) -> ToolExecutionContext:
    return ToolExecutionContext(
        actor=AuthenticatedActor(
            actor_id="agent-process-001",
            tenant_id=tenant_id,
            roles=frozenset({Role.AGENT}),
        ),
        request_id="request-process",
        trace_id="trace-process",
        agent_run_id="run-process",
        agent_step_id="step-process",
    )


def test_real_mcp_process_discovery_call_and_tenant_isolation(
    mcp_process,
) -> None:
    runtime_settings = _settings(mcp_process)
    adapter = MCPOrderToolAdapter.from_settings(runtime_settings)
    definition = adapter.discover(_context())
    recorder = InMemoryToolCallRecorder()
    executor = ToolExecutor(ToolRegistry([definition]), recorder)
    try:
        found = executor.execute(
            ToolCallRequest(
                tool_name="order_lookup",
                tool_version="1.0.0",
                arguments={"order_id": "10086"},
                context=_context(),
            )
        )
        hidden = executor.execute(
            ToolCallRequest(
                tool_name="order_lookup",
                tool_version="1.0.0",
                arguments={"order_id": "10086"},
                context=_context(OTHER_TENANT_ID),
            )
        )
    finally:
        executor.close()

    assert isinstance(found.output, OrderObservation)
    assert found.output.status is OrderStatus.SHIPPED
    assert hidden.error is not None
    assert hidden.error.code == "tool_resource_not_found"
    assert [item.trace_id for item in recorder.observations] == [
        "trace-process",
        "trace-process",
    ]


def test_main_application_jwt_cannot_be_passed_through_to_mcp(
    mcp_process,
) -> None:
    runtime_settings = _settings(mcp_process)
    application_token = JwtTokenService(
        runtime_settings
    ).issue_access_token(
        actor_id="agent-process-001",
        tenant_id=TENANT_ID,
        roles=frozenset({Role.AGENT}),
    )
    gateway = StreamableHttpMCPGateway(
        mcp_process,
        timeout_seconds=2,
    )

    with pytest.raises(Exception):
        gateway.list_tools(application_token)


def test_valid_mcp_token_can_discover_tools_over_streamable_http(
    mcp_process,
) -> None:
    runtime_settings = _settings(mcp_process)
    token = MCPServiceTokenIssuer(runtime_settings).issue(
        _context(),
        permissions=frozenset({Permission.TOOL_ORDER_READ}),
    )
    tools = StreamableHttpMCPGateway(
        mcp_process,
        timeout_seconds=2,
    ).list_tools(token)

    assert [tool.name for tool in tools] == ["order_lookup"]
