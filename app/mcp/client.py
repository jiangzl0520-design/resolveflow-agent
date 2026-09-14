import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from threading import RLock
from typing import Any, Protocol

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, Tool
from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.mcp.contracts import (
    CircuitStateSnapshot,
    MCPToolContractError,
    META_INPUT_SCHEMA_HASH,
    META_OUTPUT_SCHEMA_HASH,
    META_REQUIRED_PERMISSION,
    META_RISK_LEVEL,
    META_SIDE_EFFECT,
    META_TOOL_VERSION,
    ORDER_TOOL_NAME,
    ORDER_TOOL_PERMISSION,
    ORDER_TOOL_SCOPE,
    ORDER_TOOL_VERSION,
    document_hash,
    monotonic_seconds,
)
from app.mcp.security import MCPServiceTokenIssuer
from app.tools.after_sales import (
    OrderLookupArguments,
    OrderObservation,
)
from app.tools.contracts import (
    ToolDefinition,
    ToolExecutionContext,
    ToolRiskLevel,
    ToolSideEffect,
)
from app.tools.errors import (
    ToolCircuitOpenError,
    ToolDependencyUnavailableError,
    ToolRemoteAuthorizationError,
    ToolRemoteContractError,
    ToolRemoteTimeoutError,
    ToolResourceNotFoundError,
)

MAX_TOOL_DISCOVERY_PAGES = 10
MAX_DISCOVERED_TOOLS = 256


class MCPGateway(Protocol):
    def list_tools(self, token: str) -> list[Tool]: ...

    def call_tool(
        self,
        token: str,
        *,
        name: str,
        arguments: dict[str, Any],
    ) -> CallToolResult: ...


class StreamableHttpMCPGateway:
    """A small synchronous boundary around the asynchronous MCP client SDK."""

    def __init__(self, url: str, *, timeout_seconds: float) -> None:
        self._url = url
        self._timeout_seconds = timeout_seconds

    def list_tools(self, token: str) -> list[Tool]:
        return _run_sync(self._list_tools(token))

    def call_tool(
        self,
        token: str,
        *,
        name: str,
        arguments: dict[str, Any],
    ) -> CallToolResult:
        return _run_sync(
            self._call_tool(
                token,
                name=name,
                arguments=arguments,
            )
        )

    async def _list_tools(self, token: str) -> list[Tool]:
        async with self._client(token) as client:
            page = await client.list_tools(cache_mode="reload")
            tools = list(page.tools)
            pages = 1
            while page.next_cursor is not None:
                if pages >= MAX_TOOL_DISCOVERY_PAGES:
                    raise MCPToolContractError(
                        "Remote tool discovery exceeded its page budget."
                    )
                page = await client.list_tools(
                    cursor=page.next_cursor,
                    cache_mode="reload",
                )
                tools.extend(page.tools)
                pages += 1
                if len(tools) > MAX_DISCOVERED_TOOLS:
                    raise MCPToolContractError(
                        "Remote tool discovery exceeded its tool budget."
                    )
            return tools

    async def _call_tool(
        self,
        token: str,
        *,
        name: str,
        arguments: dict[str, Any],
    ) -> CallToolResult:
        async with self._client(token) as client:
            return await client.call_tool(
                name,
                arguments,
                read_timeout_seconds=self._timeout_seconds,
            )

    @asynccontextmanager
    async def _client(self, token: str):
        timeout = httpx2.Timeout(self._timeout_seconds)
        async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            follow_redirects=False,
            # Service credentials must not be inherited by an ambient system
            # proxy. A production proxy must be configured explicitly as a
            # trusted transport dependency.
            trust_env=False,
        ) as http_client:
            transport = streamable_http_client(
                self._url,
                http_client=http_client,
                terminate_on_close=False,
            )
            async with Client(
                transport,
                read_timeout_seconds=self._timeout_seconds,
            ) as client:
                yield client


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int,
        recovery_seconds: float,
        clock: Callable[[], float] = monotonic_seconds,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be positive.")
        if recovery_seconds <= 0:
            raise ValueError("recovery_seconds must be positive.")
        self._failure_threshold = failure_threshold
        self._recovery_seconds = recovery_seconds
        self._clock = clock
        self._state = "closed"
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._lock = RLock()

    def before_call(self) -> None:
        with self._lock:
            if self._state == "closed":
                return
            if self._state == "half_open":
                raise ToolCircuitOpenError()
            assert self._opened_at is not None
            if self._clock() - self._opened_at >= self._recovery_seconds:
                self._state = "half_open"
                return
            raise ToolCircuitOpenError()

    def record_success(self) -> None:
        with self._lock:
            self._state = "closed"
            self._consecutive_failures = 0
            self._opened_at = None

    def record_dependency_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if (
                self._state == "half_open"
                or self._consecutive_failures
                >= self._failure_threshold
            ):
                self._state = "open"
                self._opened_at = self._clock()

    @property
    def snapshot(self) -> CircuitStateSnapshot:
        with self._lock:
            return CircuitStateSnapshot(
                state=self._state,
                consecutive_failures=self._consecutive_failures,
                opened_at=self._opened_at,
            )


class MCPOrderToolAdapter:
    """Discovers one trusted remote tool and exposes the local ToolDefinition."""

    def __init__(
        self,
        gateway: MCPGateway,
        token_issuer: MCPServiceTokenIssuer,
        *,
        timeout_seconds: float,
        circuit_breaker: CircuitBreaker,
    ) -> None:
        self._gateway = gateway
        self._token_issuer = token_issuer
        self._timeout_seconds = timeout_seconds
        self._circuit_breaker = circuit_breaker

    @classmethod
    def from_settings(cls, settings: Settings) -> "MCPOrderToolAdapter":
        return cls(
            StreamableHttpMCPGateway(
                settings.mcp_order_server_url,
                timeout_seconds=settings.mcp_timeout_seconds,
            ),
            MCPServiceTokenIssuer(settings),
            timeout_seconds=settings.mcp_timeout_seconds,
            circuit_breaker=CircuitBreaker(
                failure_threshold=(
                    settings.mcp_circuit_failure_threshold
                ),
                recovery_seconds=settings.mcp_circuit_recovery_seconds,
            ),
        )

    def discover(
        self,
        context: ToolExecutionContext,
    ) -> ToolDefinition:
        token = self._issue_token(context)
        try:
            tools = self._gateway.list_tools(token)
        except MCPToolContractError:
            raise
        except Exception as exc:
            _raise_gateway_error(exc)

        matches = [tool for tool in tools if tool.name == ORDER_TOOL_NAME]
        if len(matches) != 1:
            raise MCPToolContractError(
                "Expected exactly one order_lookup tool."
            )
        _validate_discovered_tool(matches[0])
        return ToolDefinition(
            name=ORDER_TOOL_NAME,
            version=ORDER_TOOL_VERSION,
            description=(
                "Read the tenant-scoped payment and fulfillment state "
                "through the independent ResolveFlow MCP order service."
            ),
            input_model=OrderLookupArguments,
            output_model=OrderObservation,
            risk_level=ToolRiskLevel.LOW,
            side_effect=ToolSideEffect.READ_ONLY,
            required_permission=ORDER_TOOL_PERMISSION,
            timeout_seconds=self._timeout_seconds,
            handler=self._call,
        )

    def _call(
        self,
        arguments: BaseModel,
        context: ToolExecutionContext,
    ) -> OrderObservation:
        parsed = OrderLookupArguments.model_validate(arguments)
        self._circuit_breaker.before_call()
        token = self._issue_token(context)
        try:
            result = self._gateway.call_tool(
                token,
                name=ORDER_TOOL_NAME,
                arguments=parsed.model_dump(mode="json"),
            )
        except MCPError as exc:
            mapped = _mapped_mcp_error(exc)
            if isinstance(mapped, ToolDependencyUnavailableError):
                self._circuit_breaker.record_dependency_failure()
            else:
                self._circuit_breaker.record_success()
            raise mapped from exc
        except Exception as exc:
            protocol_error = _find_exception(exc, MCPError)
            mapped = (
                _mapped_mcp_error(protocol_error)
                if isinstance(protocol_error, MCPError)
                else _gateway_tool_error(exc)
            )
            if isinstance(
                mapped,
                (ToolDependencyUnavailableError, ToolRemoteTimeoutError),
            ):
                self._circuit_breaker.record_dependency_failure()
            raise mapped from exc

        if result.is_error:
            self._circuit_breaker.record_success()
            raise ToolRemoteContractError()
        try:
            output = OrderObservation.model_validate(
                result.structured_content
            )
        except (ValidationError, TypeError, ValueError) as exc:
            self._circuit_breaker.record_success()
            raise ToolRemoteContractError() from exc
        self._circuit_breaker.record_success()
        return output

    def _issue_token(self, context: ToolExecutionContext) -> str:
        try:
            return self._token_issuer.issue(
                context,
                permissions=frozenset({ORDER_TOOL_PERMISSION}),
            )
        except PermissionError as exc:
            raise ToolRemoteAuthorizationError() from exc


def _validate_discovered_tool(tool: Tool) -> None:
    metadata = tool.meta or {}
    expected_metadata = {
        META_TOOL_VERSION: ORDER_TOOL_VERSION,
        META_RISK_LEVEL: ToolRiskLevel.LOW.value,
        META_SIDE_EFFECT: ToolSideEffect.READ_ONLY.value,
        META_REQUIRED_PERMISSION: ORDER_TOOL_SCOPE,
        META_INPUT_SCHEMA_HASH: document_hash(
            OrderLookupArguments.model_json_schema()
        ),
        META_OUTPUT_SCHEMA_HASH: document_hash(
            OrderObservation.model_json_schema()
        ),
    }
    for key, expected in expected_metadata.items():
        if metadata.get(key) != expected:
            raise MCPToolContractError(
                f"Remote tool metadata mismatch for {key}."
            )

    annotations = tool.annotations
    if (
        annotations is None
        or annotations.read_only_hint is not True
        or annotations.destructive_hint is not False
        or annotations.idempotent_hint is not True
        or annotations.open_world_hint is not False
    ):
        raise MCPToolContractError(
            "Remote order tool annotations do not match the read-only contract."
        )

    properties = tool.input_schema.get("properties")
    order_id = (
        properties.get("order_id")
        if isinstance(properties, dict)
        else None
    )
    if (
        set(properties or {}) != {"order_id"}
        or tool.input_schema.get("required") != ["order_id"]
        or not isinstance(order_id, dict)
        or order_id.get("type") != "string"
        or order_id.get("minLength") != 3
        or order_id.get("maxLength") != 64
        or order_id.get("pattern") != r"^[A-Za-z0-9_-]+$"
    ):
        raise MCPToolContractError(
            "Remote order tool input schema is incompatible."
        )

    if tool.output_schema is None:
        raise MCPToolContractError(
            "Remote order tool did not publish an output schema."
        )
    if document_hash(tool.output_schema) != expected_metadata[
        META_OUTPUT_SCHEMA_HASH
    ]:
        raise MCPToolContractError(
            "Remote order tool output schema is incompatible."
        )


def _mapped_mcp_error(exc: MCPError) -> Exception:
    data = exc.data if isinstance(exc.data, dict) else {}
    code = data.get("resolveflow_code")
    if code == "tool_resource_not_found":
        return ToolResourceNotFoundError()
    if code == "tool_dependency_unavailable":
        return ToolDependencyUnavailableError()
    if code in {
        "mcp_authentication_required",
        "mcp_permission_denied",
    }:
        return ToolRemoteAuthorizationError()
    return ToolDependencyUnavailableError()


def _gateway_tool_error(exc: Exception) -> Exception:
    if _has_http_status(exc, {401, 403}):
        return ToolRemoteAuthorizationError()
    if _find_exception(
        exc,
        (
            TimeoutError,
            asyncio.TimeoutError,
            httpx2.TimeoutException,
        ),
    ):
        return ToolRemoteTimeoutError()
    return ToolDependencyUnavailableError()


def _raise_gateway_error(exc: Exception) -> None:
    mapped = _gateway_tool_error(exc)
    raise mapped from exc


def _has_http_status(exc: BaseException, statuses: set[int]) -> bool:
    for current in _walk_exceptions(exc):
        response = getattr(current, "response", None)
        if getattr(response, "status_code", None) in statuses:
            return True
    return False


def _find_exception(
    exc: BaseException,
    exception_type,
) -> BaseException | None:
    return next(
        (
            item
            for item in _walk_exceptions(exc)
            if isinstance(item, exception_type)
        ),
        None,
    )


def _walk_exceptions(exc: BaseException):
    pending = [exc]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        yield current
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)


def _run_sync(awaitable):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    close = getattr(awaitable, "close", None)
    if close is not None:
        close()
    raise RuntimeError(
        "The synchronous MCP gateway cannot run inside an event loop."
    )
