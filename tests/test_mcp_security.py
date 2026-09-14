import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.core.config import AppEnvironment, Settings
from app.domain.auth import AuthenticatedActor, Permission, Role
from app.mcp.contracts import MCP_AGENT_CLIENT_ID, ORDER_TOOL_SCOPE
from app.mcp.security import (
    MCPServiceTokenIssuer,
    MCPServiceTokenVerifier,
)
from app.tools.contracts import ToolExecutionContext

TENANT_ID = UUID("71000000-0000-0000-0000-000000000001")
SECRET = "day11-test-mcp-secret-that-is-longer-than-thirty-two-chars"
RESOURCE = "http://127.0.0.1:18011/mcp"


def settings() -> Settings:
    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        environment=AppEnvironment.TEST,
        mcp_jwt_secret=SECRET,
        mcp_order_server_url=RESOURCE,
    )


def context(role: Role = Role.AGENT) -> ToolExecutionContext:
    return ToolExecutionContext(
        actor=AuthenticatedActor(
            actor_id="agent-mcp-001",
            tenant_id=TENANT_ID,
            roles=frozenset({role}),
        ),
        request_id="request-mcp-security",
        trace_id="trace-mcp-security",
        agent_run_id="run-mcp-security",
        agent_step_id="step-mcp-security",
    )


def test_service_token_is_short_lived_scoped_and_traceable() -> None:
    current = datetime.now(UTC)
    token = MCPServiceTokenIssuer(settings()).issue(
        context(),
        permissions=frozenset({Permission.TOOL_ORDER_READ}),
        now=current,
    )

    verified = asyncio.run(
        MCPServiceTokenVerifier(settings()).verify_token(token)
    )

    assert verified is not None
    assert verified.client_id == MCP_AGENT_CLIENT_ID
    assert verified.scopes == [ORDER_TOOL_SCOPE]
    assert verified.subject == "agent-mcp-001"
    assert verified.claims is not None
    assert verified.claims["tenant_id"] == str(TENANT_ID)
    assert verified.claims["request_id"] == "request-mcp-security"
    assert verified.claims["trace_id"] == "trace-mcp-security"
    assert verified.expires_at == int((current + timedelta(seconds=60)).timestamp())


def test_wrong_audience_and_expired_tokens_are_rejected() -> None:
    now = datetime.now(UTC)
    wrong_audience = MCPServiceTokenIssuer(
        settings(),
        audience="http://127.0.0.1:18012/mcp",
    ).issue(
        context(),
        permissions=frozenset({Permission.TOOL_ORDER_READ}),
        now=now,
    )
    expired = MCPServiceTokenIssuer(settings()).issue(
        context(),
        permissions=frozenset({Permission.TOOL_ORDER_READ}),
        now=now - timedelta(minutes=2),
        expires_delta=timedelta(seconds=1),
    )
    verifier = MCPServiceTokenVerifier(settings())

    assert asyncio.run(verifier.verify_token(wrong_audience)) is None
    assert asyncio.run(verifier.verify_token(expired)) is None


def test_actor_cannot_delegate_a_permission_it_does_not_have() -> None:
    with pytest.raises(PermissionError):
        MCPServiceTokenIssuer(settings()).issue(
            context(Role.CUSTOMER),
            permissions=frozenset({Permission.TOOL_ORDER_READ}),
        )


def test_production_rejects_the_development_mcp_secret() -> None:
    with pytest.raises(
        ValueError,
        match="development MCP JWT secret",
    ):
        Settings(
            database_url="postgresql+psycopg://production/resolveflow",
            environment=AppEnvironment.PRODUCTION,
            jwt_secret=(
                "production-main-jwt-secret-longer-than-thirty-two"
            ),
        )
