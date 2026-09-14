from dataclasses import dataclass
from hashlib import sha256
import json
from time import monotonic
from threading import RLock
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.domain.auth import Permission
from app.tools.after_sales import (
    OrderLookupArguments,
    OrderObservation,
    TOOL_VERSION,
)
from app.tools.contracts import ToolRiskLevel, ToolSideEffect

ORDER_TOOL_NAME = "order_lookup"
ORDER_TOOL_VERSION = TOOL_VERSION
ORDER_TOOL_PERMISSION = Permission.TOOL_ORDER_READ
ORDER_TOOL_SCOPE = Permission.TOOL_ORDER_READ.value
MCP_AGENT_CLIENT_ID = "resolveflow-agent-runtime"

META_TOOL_VERSION = "resolveflow/toolVersion"
META_RISK_LEVEL = "resolveflow/riskLevel"
META_SIDE_EFFECT = "resolveflow/sideEffect"
META_REQUIRED_PERMISSION = "resolveflow/requiredPermission"
META_INPUT_SCHEMA_HASH = "resolveflow/inputSchemaHash"
META_OUTPUT_SCHEMA_HASH = "resolveflow/outputSchemaHash"


def document_hash(document: dict[str, Any]) -> str:
    serialized = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(serialized).hexdigest()


def order_tool_metadata() -> dict[str, str]:
    return {
        META_TOOL_VERSION: ORDER_TOOL_VERSION,
        META_RISK_LEVEL: ToolRiskLevel.LOW.value,
        META_SIDE_EFFECT: ToolSideEffect.READ_ONLY.value,
        META_REQUIRED_PERMISSION: ORDER_TOOL_PERMISSION.value,
        META_INPUT_SCHEMA_HASH: document_hash(
            OrderLookupArguments.model_json_schema()
        ),
        META_OUTPUT_SCHEMA_HASH: document_hash(
            OrderObservation.model_json_schema()
        ),
    }


class MCPToolContractError(RuntimeError):
    """The discovered remote tool does not match the local trusted contract."""


class MCPRemoteAuthorizationError(RuntimeError):
    """The MCP resource server rejected the service identity."""


@dataclass(frozen=True, slots=True)
class MCPCallAuditRecord:
    tenant_id: UUID
    actor_id: str
    tool_name: str
    tool_version: str
    request_id: str
    trace_id: str
    agent_run_id: str | None
    agent_step_id: str | None
    outcome: str
    error_code: str | None
    duration_ms: float


class InMemoryMCPAuditSink:
    def __init__(self) -> None:
        self.records: list[MCPCallAuditRecord] = []
        self._lock = RLock()

    def add(self, record: MCPCallAuditRecord) -> None:
        with self._lock:
            self.records.append(record)


class MCPErrorData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolveflow_code: str
    retryable: bool


class CircuitStateSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: str
    consecutive_failures: int
    opened_at: float | None


def monotonic_seconds() -> float:
    return monotonic()
