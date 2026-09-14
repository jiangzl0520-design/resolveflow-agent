from time import perf_counter
from typing import Annotated, Any, Protocol
from uuid import UUID

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError
from mcp.types import ToolAnnotations
from pydantic import Field

from app.core.config import Settings
from app.mcp.contracts import (
    InMemoryMCPAuditSink,
    MCPCallAuditRecord,
    ORDER_TOOL_NAME,
    ORDER_TOOL_SCOPE,
    ORDER_TOOL_VERSION,
    order_tool_metadata,
)
from app.mcp.security import MCPServiceTokenVerifier
from app.tools.after_sales import (
    InMemoryAfterSalesDataSource,
    OrderObservation,
    OrderStatus,
)
from app.tools.errors import (
    ToolDependencyUnavailableError,
    ToolResourceNotFoundError,
)

MCP_RESOURCE_NOT_FOUND = -32041
MCP_DEPENDENCY_UNAVAILABLE = -32042


class MCPAuditSink(Protocol):
    def add(self, record: MCPCallAuditRecord) -> None: ...


def build_order_mcp_server(
    source: InMemoryAfterSalesDataSource,
    settings: Settings,
    *,
    audience: str | None = None,
    audit_sink: MCPAuditSink | None = None,
) -> MCPServer:
    resource_url = audience or settings.mcp_order_server_url
    sink = audit_sink or InMemoryMCPAuditSink()
    verifier = MCPServiceTokenVerifier(
        settings,
        audience=resource_url,
    )
    server = MCPServer(
        name="resolveflow-order-tools",
        version=ORDER_TOOL_VERSION,
        token_verifier=verifier,
        auth=AuthSettings(
            issuer_url=settings.mcp_jwt_issuer,
            resource_server_url=resource_url,
            required_scopes=[ORDER_TOOL_SCOPE],
        ),
    )

    @server.tool(
        name=ORDER_TOOL_NAME,
        description=(
            "Read the authenticated tenant's payment and fulfillment state "
            "for one exact order identifier."
        ),
        annotations=ToolAnnotations(
            title="ResolveFlow order lookup",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        meta=order_tool_metadata(),
        structured_output=True,
    )
    async def order_lookup(
        order_id: Annotated[
            str,
            Field(
                min_length=3,
                max_length=64,
                pattern=r"^[A-Za-z0-9_-]+$",
            ),
        ],
    ) -> OrderObservation:
        token = _require_identity(get_access_token())
        claims = token.claims or {}
        tenant_id = UUID(str(claims["tenant_id"]))
        started = perf_counter()
        outcome = "succeeded"
        error_code: str | None = None
        try:
            order = source.get_order(tenant_id, order_id)
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
        except ToolResourceNotFoundError as exc:
            outcome = "failed"
            error_code = "tool_resource_not_found"
            raise MCPError(
                MCP_RESOURCE_NOT_FOUND,
                "The requested resource was not found.",
                {
                    "resolveflow_code": error_code,
                    "retryable": False,
                },
            ) from exc
        except ToolDependencyUnavailableError as exc:
            outcome = "failed"
            error_code = "tool_dependency_unavailable"
            raise MCPError(
                MCP_DEPENDENCY_UNAVAILABLE,
                "The order dependency is temporarily unavailable.",
                {
                    "resolveflow_code": error_code,
                    "retryable": True,
                },
            ) from exc
        except Exception:
            outcome = "failed"
            error_code = "tool_execution_failed"
            raise
        finally:
            sink.add(
                _audit_record(
                    token,
                    outcome=outcome,
                    error_code=error_code,
                    duration_ms=(perf_counter() - started) * 1000,
                )
            )

    return server


def _require_identity(token: AccessToken | None) -> AccessToken:
    if token is None:
        raise MCPError(
            -32040,
            "MCP service authentication is required.",
            {
                "resolveflow_code": "mcp_authentication_required",
                "retryable": False,
            },
        )
    claims = token.claims or {}
    if (
        ORDER_TOOL_SCOPE not in token.scopes
        or not claims.get("tenant_id")
        or not claims.get("actor_id")
    ):
        raise MCPError(
            -32040,
            "MCP service identity is not authorized.",
            {
                "resolveflow_code": "mcp_permission_denied",
                "retryable": False,
            },
        )
    return token


def _audit_record(
    token: AccessToken,
    *,
    outcome: str,
    error_code: str | None,
    duration_ms: float,
) -> MCPCallAuditRecord:
    claims: dict[str, Any] = token.claims or {}
    return MCPCallAuditRecord(
        tenant_id=UUID(str(claims["tenant_id"])),
        actor_id=str(claims["actor_id"]),
        tool_name=ORDER_TOOL_NAME,
        tool_version=ORDER_TOOL_VERSION,
        request_id=str(claims["request_id"]),
        trace_id=str(claims["trace_id"]),
        agent_run_id=claims.get("agent_run_id"),
        agent_step_id=claims.get("agent_step_id"),
        outcome=outcome,
        error_code=error_code,
        duration_ms=duration_ms,
    )
