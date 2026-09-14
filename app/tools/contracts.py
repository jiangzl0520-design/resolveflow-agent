from dataclasses import dataclass
from enum import StrEnum
from re import fullmatch
from typing import Any, Callable
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.domain.auth import AuthenticatedActor, Permission


class ToolRiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ToolSideEffect(StrEnum):
    READ_ONLY = "read_only"
    WRITE = "write"


class ToolCallStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ToolFailureKind(StrEnum):
    SELECTION = "selection_error"
    ARGUMENT = "argument_error"
    AUTHORIZATION = "authorization_error"
    TIMEOUT = "timeout_error"
    DEPENDENCY = "dependency_error"
    RESOURCE = "resource_error"
    CONFLICT = "conflict_error"
    OUTPUT_CONTRACT = "output_contract_error"
    INTERNAL = "internal_error"


class ToolErrorDetail(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ToolFailureKind
    code: str
    retryable: bool
    message: str
    validation_errors: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    actor: AuthenticatedActor
    request_id: str
    trace_id: str
    agent_run_id: str | None = None
    agent_step_id: str | None = None

    @property
    def tenant_id(self) -> UUID:
        return self.actor.tenant_id


ToolHandler = Callable[
    [BaseModel, ToolExecutionContext],
    BaseModel | dict[str, Any],
]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    version: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    risk_level: ToolRiskLevel
    side_effect: ToolSideEffect
    required_permission: Permission
    timeout_seconds: float
    handler: ToolHandler

    def __post_init__(self) -> None:
        if not fullmatch(r"[A-Za-z0-9_-]{1,64}", self.name):
            raise ValueError("Tool name contains unsupported characters.")
        if not fullmatch(r"\d+\.\d+\.\d+", self.version):
            raise ValueError("Tool version must use semantic x.y.z form.")
        if not self.description:
            raise ValueError("Tool description must not be empty.")
        if self.timeout_seconds <= 0:
            raise ValueError("Tool timeout must be positive.")

    @property
    def input_schema(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()

    @property
    def output_schema(self) -> dict[str, Any]:
        return self.output_model.model_json_schema()


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    name: str
    version: str
    description: str
    input_schema: dict[str, Any]
    risk_level: ToolRiskLevel
    side_effect: ToolSideEffect


@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    tool_name: str
    tool_version: str
    arguments: dict[str, Any]
    context: ToolExecutionContext


@dataclass(frozen=True, slots=True)
class ToolObservation:
    call_id: UUID
    tenant_id: UUID
    actor_id: str
    tool_name: str
    tool_version: str
    input_schema_hash: str | None
    output_schema_hash: str | None
    status: ToolCallStatus
    risk_level: ToolRiskLevel | None
    side_effect: ToolSideEffect | None
    output: BaseModel | None
    error: ToolErrorDetail | None
    arguments_hash: str
    duration_ms: float
    request_id: str
    trace_id: str
    agent_run_id: str | None
    agent_step_id: str | None

    @property
    def succeeded(self) -> bool:
        return self.status is ToolCallStatus.SUCCEEDED
