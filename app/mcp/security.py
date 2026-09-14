from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import jwt
from jwt.exceptions import InvalidTokenError
from mcp.server.auth.provider import AccessToken

from app.core.config import Settings
from app.domain.auth import Permission
from app.mcp.contracts import MCP_AGENT_CLIENT_ID
from app.tools.contracts import ToolExecutionContext

MCP_JWT_ALGORITHM = "HS256"


class MCPServiceTokenIssuer:
    """Mints a short-lived, audience-bound token for one MCP tool call."""

    def __init__(
        self,
        settings: Settings,
        *,
        audience: str | None = None,
    ) -> None:
        self._secret = settings.mcp_jwt_secret
        self._issuer = settings.mcp_jwt_issuer
        self._audience = audience or settings.mcp_order_server_url
        self._lifetime = timedelta(
            seconds=settings.mcp_token_expire_seconds
        )

    def issue(
        self,
        context: ToolExecutionContext,
        *,
        permissions: frozenset[Permission],
        now: datetime | None = None,
        expires_delta: timedelta | None = None,
    ) -> str:
        if not permissions:
            raise ValueError("At least one MCP permission is required.")
        if not all(
            context.actor.has_permission(permission)
            for permission in permissions
        ):
            raise PermissionError(
                "The actor cannot delegate an MCP permission it does not have."
            )

        issued_at = now or datetime.now(UTC)
        expires_at = issued_at + (
            expires_delta
            if expires_delta is not None
            else self._lifetime
        )
        claims = {
            "iss": self._issuer,
            "aud": self._audience,
            "sub": context.actor.actor_id,
            "client_id": MCP_AGENT_CLIENT_ID,
            "tenant_id": str(context.tenant_id),
            "scope": " ".join(
                sorted(permission.value for permission in permissions)
            ),
            "request_id": context.request_id,
            "trace_id": context.trace_id,
            "agent_run_id": context.agent_run_id,
            "agent_step_id": context.agent_step_id,
            "iat": issued_at,
            "nbf": issued_at,
            "exp": expires_at,
            "jti": str(uuid4()),
        }
        return jwt.encode(
            claims,
            self._secret,
            algorithm=MCP_JWT_ALGORITHM,
        )


class MCPServiceTokenVerifier:
    """Validates only tokens minted for this exact MCP resource server."""

    def __init__(
        self,
        settings: Settings,
        *,
        audience: str | None = None,
    ) -> None:
        self._secret = settings.mcp_jwt_secret
        self._issuer = settings.mcp_jwt_issuer
        self._audience = audience or settings.mcp_order_server_url

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=[MCP_JWT_ALGORITHM],
                audience=self._audience,
                issuer=self._issuer,
                options={
                    "require": [
                        "iss",
                        "aud",
                        "sub",
                        "client_id",
                        "tenant_id",
                        "scope",
                        "request_id",
                        "trace_id",
                        "iat",
                        "nbf",
                        "exp",
                        "jti",
                    ]
                },
            )
            parsed = _validated_claims(claims)
        except (InvalidTokenError, KeyError, TypeError, ValueError):
            return None

        return AccessToken(
            token=token,
            client_id=MCP_AGENT_CLIENT_ID,
            scopes=parsed["scopes"],
            expires_at=int(parsed["exp"]),
            resource=self._audience,
            subject=parsed["actor_id"],
            claims=parsed,
        )


def _validated_claims(claims: dict[str, Any]) -> dict[str, Any]:
    actor_id = claims["sub"]
    client_id = claims["client_id"]
    tenant_id = UUID(str(claims["tenant_id"]))
    raw_scope = claims["scope"]
    request_id = claims["request_id"]
    trace_id = claims["trace_id"]
    expires_at = claims["exp"]
    jti = claims["jti"]

    if not isinstance(actor_id, str) or not 1 <= len(actor_id) <= 128:
        raise ValueError("Invalid MCP subject.")
    if client_id != MCP_AGENT_CLIENT_ID:
        raise ValueError("Invalid MCP client.")
    if tenant_id.int == 0:
        raise ValueError("Invalid MCP tenant.")
    if not isinstance(raw_scope, str) or not raw_scope.strip():
        raise ValueError("Invalid MCP scope.")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("Invalid MCP request id.")
    if not isinstance(trace_id, str) or not trace_id:
        raise ValueError("Invalid MCP trace id.")
    if not isinstance(expires_at, int | float):
        raise ValueError("Invalid MCP expiry.")
    if not isinstance(jti, str) or UUID(jti).int == 0:
        raise ValueError("Invalid MCP token id.")

    scopes = raw_scope.split()
    if len(scopes) != len(set(scopes)):
        raise ValueError("Duplicate MCP scopes.")
    for key in ("agent_run_id", "agent_step_id"):
        if claims.get(key) is not None and not isinstance(
            claims[key],
            str,
        ):
            raise ValueError(f"Invalid MCP {key}.")

    return {
        "actor_id": actor_id,
        "tenant_id": str(tenant_id),
        "scopes": scopes,
        "request_id": request_id,
        "trace_id": trace_id,
        "agent_run_id": claims.get("agent_run_id"),
        "agent_step_id": claims.get("agent_step_id"),
        "exp": expires_at,
        "jti": jti,
    }
