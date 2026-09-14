from uuid import UUID

from pydantic import BaseModel, ConfigDict
import pytest

from app.domain.auth import AuthenticatedActor, Permission, Role
from app.tools.contracts import (
    ToolCallRequest,
    ToolExecutionContext,
    ToolFailureKind,
    ToolRiskLevel,
    ToolSideEffect,
    ToolDefinition,
)
from app.tools.errors import ToolCallRecordingError, ToolNotFoundError
from app.tools.executor import (
    InMemoryToolCallRecorder,
    ToolExecutor,
)
from app.tools.registry import ToolRegistry

TENANT_ID = UUID("70000000-0000-0000-0000-000000000001")


class InputValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: int


class OutputValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    doubled: int


def actor(
    role: Role = Role.AGENT,
) -> AuthenticatedActor:
    return AuthenticatedActor(
        actor_id="tool-test-actor",
        tenant_id=TENANT_ID,
        roles=frozenset({role}),
    )


def definition(
    *,
    handler=lambda arguments, context: {
        "doubled": arguments.value * 2
    },
) -> ToolDefinition:
    return ToolDefinition(
        name="test_lookup",
        version="1.0.0",
        description="Read one test value.",
        input_model=InputValue,
        output_model=OutputValue,
        risk_level=ToolRiskLevel.LOW,
        side_effect=ToolSideEffect.READ_ONLY,
        required_permission=Permission.TOOL_ORDER_READ,
        timeout_seconds=1,
        handler=handler,
    )


def request(
    *,
    name: str = "test_lookup",
    version: str = "1.0.0",
    arguments=None,
    current_actor=None,
) -> ToolCallRequest:
    return ToolCallRequest(
        tool_name=name,
        tool_version=version,
        arguments=arguments or {"value": 3},
        context=ToolExecutionContext(
            actor=current_actor or actor(),
            request_id="request-tool-001",
            trace_id="trace-tool-001",
            agent_run_id="run-001",
            agent_step_id="step-001",
        ),
    )


def test_registry_exposes_only_authorized_versioned_contracts() -> None:
    registry = ToolRegistry([definition()])

    descriptors = registry.descriptors_for(actor())

    assert len(descriptors) == 1
    descriptor = descriptors[0]
    assert descriptor.name == "test_lookup"
    assert descriptor.version == "1.0.0"
    assert descriptor.side_effect is ToolSideEffect.READ_ONLY
    assert descriptor.input_schema["additionalProperties"] is False
    assert registry.descriptors_for(actor(Role.CUSTOMER)) == ()
    with pytest.raises(ToolNotFoundError):
        registry.resolve("test_lookup", "2.0.0")


def test_registry_rejects_duplicate_name_and_version() -> None:
    with pytest.raises(ValueError):
        ToolRegistry([definition(), definition()])


def test_tool_identity_rejects_unsafe_name_and_ambiguous_version() -> None:
    original = definition()
    with pytest.raises(ValueError):
        ToolDefinition(
            name="tool with spaces",
            version=original.version,
            description=original.description,
            input_model=original.input_model,
            output_model=original.output_model,
            risk_level=original.risk_level,
            side_effect=original.side_effect,
            required_permission=original.required_permission,
            timeout_seconds=original.timeout_seconds,
            handler=original.handler,
        )
    with pytest.raises(ValueError):
        ToolDefinition(
            name=original.name,
            version="latest",
            description=original.description,
            input_model=original.input_model,
            output_model=original.output_model,
            risk_level=original.risk_level,
            side_effect=original.side_effect,
            required_permission=original.required_permission,
            timeout_seconds=original.timeout_seconds,
            handler=original.handler,
        )


def test_executor_returns_typed_observation_and_trace_metadata() -> None:
    recorder = InMemoryToolCallRecorder()
    executor = ToolExecutor(ToolRegistry([definition()]), recorder)
    try:
        observation = executor.execute(request())
    finally:
        executor.close()

    assert observation.succeeded is True
    assert observation.output == OutputValue(doubled=6)
    assert observation.error is None
    assert observation.trace_id == "trace-tool-001"
    assert observation.agent_run_id == "run-001"
    assert observation.tenant_id == TENANT_ID
    assert observation.actor_id == "tool-test-actor"
    assert len(observation.arguments_hash) == 64
    assert len(observation.input_schema_hash or "") == 64
    assert len(observation.output_schema_hash or "") == 64
    assert recorder.observations == [observation]


def test_unknown_tool_is_a_selection_error() -> None:
    executor = ToolExecutor(
        ToolRegistry([definition()]),
        InMemoryToolCallRecorder(),
    )
    try:
        observation = executor.execute(
            request(name="invented_refund_tool")
        )
    finally:
        executor.close()

    assert observation.succeeded is False
    assert observation.error is not None
    assert observation.error.kind is ToolFailureKind.SELECTION
    assert observation.error.code == "tool_not_found"


def test_invalid_arguments_are_blocked_before_handler() -> None:
    calls = []

    def handler(arguments, context):
        calls.append(arguments)
        return {"doubled": 1}

    executor = ToolExecutor(
        ToolRegistry([definition(handler=handler)]),
        InMemoryToolCallRecorder(),
    )
    try:
        observation = executor.execute(
            request(
                arguments={
                    "value": "3",
                    "tenant_id": str(TENANT_ID),
                }
            )
        )
    finally:
        executor.close()

    assert observation.error is not None
    assert observation.error.kind is ToolFailureKind.ARGUMENT
    assert observation.error.code == "tool_arguments_invalid"
    assert calls == []


def test_permission_is_checked_before_handler() -> None:
    calls = []

    def handler(arguments, context):
        calls.append(arguments)
        return {"doubled": 1}

    executor = ToolExecutor(
        ToolRegistry([definition(handler=handler)]),
        InMemoryToolCallRecorder(),
    )
    try:
        observation = executor.execute(
            request(current_actor=actor(Role.CUSTOMER))
        )
    finally:
        executor.close()

    assert observation.error is not None
    assert observation.error.kind is ToolFailureKind.AUTHORIZATION
    assert observation.error.code == "tool_permission_denied"
    assert calls == []


def test_invalid_handler_output_is_contract_error() -> None:
    executor = ToolExecutor(
        ToolRegistry(
            [
                definition(
                    handler=lambda arguments, context: {
                        "unexpected": True
                    }
                )
            ]
        ),
        InMemoryToolCallRecorder(),
    )
    try:
        observation = executor.execute(request())
    finally:
        executor.close()

    assert observation.error is not None
    assert observation.error.kind is ToolFailureKind.OUTPUT_CONTRACT
    assert observation.error.code == "tool_output_invalid"


class FailingRecorder:
    def add(self, observation) -> None:
        raise RuntimeError("trace store unavailable")


def test_observation_recording_failure_blocks_result() -> None:
    executor = ToolExecutor(
        ToolRegistry([definition()]),
        FailingRecorder(),
    )
    try:
        with pytest.raises(ToolCallRecordingError):
            executor.execute(request())
    finally:
        executor.close()
