from uuid import UUID, uuid4

from app.domain.auth import AuthenticatedActor, Role
from app.tools.contracts import (
    ToolCallRequest,
    ToolExecutionContext,
    ToolFailureKind,
    ToolRiskLevel,
    ToolSideEffect,
)
from app.tools.executor import InMemoryToolCallRecorder, ToolExecutor
from app.tools.refund import (
    InMemoryRefundDataSource,
    RefundExecutionGuard,
    RefundObservation,
    build_refund_tools,
)
from app.tools.registry import ToolRegistry

TENANT_ID = UUID("b0000000-0000-0000-0000-000000000001")
GUARD = RefundExecutionGuard(b"day10-refund-tool-test-secret-32-bytes")


def _context(role: Role, *, actor_id: str = "refund-reviewer"):
    return ToolExecutionContext(
        actor=AuthenticatedActor(
            actor_id=actor_id,
            tenant_id=TENANT_ID,
            roles=frozenset({role}),
        ),
        request_id="request-refund-tool",
        trace_id="trace-refund-tool",
        agent_run_id="run-refund-tool",
        agent_step_id="refund-tool",
    )


def _arguments(*, amount_minor: int = 12900) -> dict:
    arguments = {
        "order_id": "10086",
        "amount_minor": amount_minor,
        "currency": "CNY",
        "reason": "Approved refund for a verified delivery dispute.",
        "proposal_hash": "a" * 64,
        "approval_id": str(uuid4()),
        "idempotency_key": "refund-idempotency-key-10086",
    }
    arguments["execution_grant"] = GUARD.issue(
        arguments,
        tenant_id=TENANT_ID,
        actor_id="refund-reviewer",
    )
    return arguments


def _call(name: str, arguments: dict, role: Role):
    return ToolCallRequest(
        tool_name=name,
        tool_version="1.0.0",
        arguments=arguments,
        context=_context(role),
    )


def test_agent_cannot_execute_refund_but_supervisor_can() -> None:
    source = InMemoryRefundDataSource()
    registry = ToolRegistry(build_refund_tools(source, GUARD))
    executor = ToolExecutor(registry, InMemoryToolCallRecorder())
    arguments = _arguments()
    try:
        denied = executor.execute(
            _call("refund_execute", arguments, Role.AGENT)
        )
        approved = executor.execute(
            _call("refund_execute", arguments, Role.SUPERVISOR)
        )
    finally:
        executor.close()

    assert denied.error is not None
    assert denied.error.kind is ToolFailureKind.AUTHORIZATION
    assert isinstance(approved.output, RefundObservation)
    assert source.execution_count == 1


def test_exact_idempotent_replay_returns_same_refund() -> None:
    source = InMemoryRefundDataSource()
    registry = ToolRegistry(build_refund_tools(source, GUARD))
    executor = ToolExecutor(registry, InMemoryToolCallRecorder())
    arguments = _arguments()
    try:
        first = executor.execute(
            _call("refund_execute", arguments, Role.SUPERVISOR)
        )
        second = executor.execute(
            _call("refund_execute", arguments, Role.SUPERVISOR)
        )
    finally:
        executor.close()

    assert first.output.refund_id == second.output.refund_id
    assert source.execution_count == 1
    assert len(source.calls) == 2


def test_same_idempotency_key_with_different_parameters_conflicts() -> None:
    source = InMemoryRefundDataSource()
    registry = ToolRegistry(build_refund_tools(source, GUARD))
    executor = ToolExecutor(registry, InMemoryToolCallRecorder())
    first_arguments = _arguments()
    changed_arguments = {
        **first_arguments,
        "amount_minor": 100,
    }
    changed_arguments["execution_grant"] = GUARD.issue(
        {
            key: value
            for key, value in changed_arguments.items()
            if key != "execution_grant"
        },
        tenant_id=TENANT_ID,
        actor_id="refund-reviewer",
    )
    try:
        executor.execute(
            _call(
                "refund_execute",
                first_arguments,
                Role.SUPERVISOR,
            )
        )
        conflict = executor.execute(
            _call(
                "refund_execute",
                changed_arguments,
                Role.SUPERVISOR,
            )
        )
    finally:
        executor.close()

    assert conflict.error is not None
    assert conflict.error.kind is ToolFailureKind.CONFLICT
    assert source.execution_count == 1


def test_refund_tools_declare_risk_and_status_is_read_only() -> None:
    definitions = {
        item.name: item
        for item in build_refund_tools(
            InMemoryRefundDataSource(),
            GUARD,
        )
    }

    assert (
        definitions["refund_execute"].risk_level
        is ToolRiskLevel.CRITICAL
    )
    assert (
        definitions["refund_execute"].side_effect
        is ToolSideEffect.WRITE
    )
    assert (
        definitions["refund_status_lookup"].side_effect
        is ToolSideEffect.READ_ONLY
    )


def test_supervisor_cannot_forge_or_omit_backend_execution_grant() -> None:
    source = InMemoryRefundDataSource()
    registry = ToolRegistry(build_refund_tools(source, GUARD))
    executor = ToolExecutor(registry, InMemoryToolCallRecorder())
    forged = {**_arguments(), "execution_grant": "0" * 64}
    try:
        denied = executor.execute(
            _call("refund_execute", forged, Role.SUPERVISOR)
        )
    finally:
        executor.close()

    assert denied.error is not None
    assert denied.error.kind is ToolFailureKind.AUTHORIZATION
    assert denied.error.code == "tool_execution_grant_invalid"
    assert source.execution_count == 0


def test_refund_tools_are_not_exposed_to_the_agent_planner() -> None:
    registry = ToolRegistry(
        build_refund_tools(InMemoryRefundDataSource(), GUARD)
    )
    agent = _context(Role.AGENT).actor
    supervisor = _context(Role.SUPERVISOR).actor

    assert registry.descriptors_for(agent) == ()
    assert {
        item.name for item in registry.descriptors_for(supervisor)
    } == {"refund_execute", "refund_status_lookup"}
