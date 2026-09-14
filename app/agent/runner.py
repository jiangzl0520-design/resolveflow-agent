from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
import json
from time import perf_counter
from collections.abc import Callable
from uuid import uuid4

from app.agent.errors import AgentPlannerError
from app.agent.models import (
    AgentActionKind,
    AgentBudget,
    AgentRunCommand,
    AgentRunState,
    AgentRunStatus,
    AgentStepTrace,
    AgentTerminationReason,
)
from app.agent.planner import AGENT_PROMPT, AgentPlanner
from app.agent.security import AgentDecisionSecurityPolicy
from app.agent.verifier import InvestigationCompletionVerifier
from app.tools.contracts import (
    ToolCallRequest,
    ToolExecutionContext,
    ToolFailureKind,
)
from app.tools.errors import ToolCallRecordingError
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry
from app.observability.tracing import (
    FailureDomain,
    add_safe_event,
    identifier_hash,
    mark_span_error,
    operation_span,
    set_safe_attributes,
)
from app.observability.metrics import get_metrics


class AgentRunner:
    def __init__(
        self,
        planner: AgentPlanner,
        registry: ToolRegistry,
        tool_executor: ToolExecutor,
        verifier: InvestigationCompletionVerifier | None = None,
        decision_security_policy: AgentDecisionSecurityPolicy | None = None,
        *,
        budget: AgentBudget | None = None,
        wall_clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] = perf_counter,
    ) -> None:
        self._planner = planner
        self._registry = registry
        self._tool_executor = tool_executor
        self._verifier = verifier or InvestigationCompletionVerifier()
        self._decision_security_policy = (
            decision_security_policy
            or AgentDecisionSecurityPolicy(
                protected_values=(AGENT_PROMPT.instructions,)
            )
        )
        self._budget = budget or AgentBudget()
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))
        self._monotonic_clock = monotonic_clock

    def run(self, command: AgentRunCommand) -> AgentRunState:
        with operation_span(
            "run after-sales agent",
            failure_domain=FailureDomain.AGENT,
            attributes={
                "resolveflow.component": "agent",
                "resolveflow.agent.workflow": "agent_runner",
                "resolveflow.agent.ticket_hash": identifier_hash(
                    command.ticket_id
                ),
                "resolveflow.agent.order_hash": identifier_hash(
                    command.order_id
                ),
                "resolveflow.agent.category": command.category.value,
                "resolveflow.agent.max_steps": self._budget.max_steps,
                "resolveflow.request_id": command.request_id,
                "resolveflow.trace_id": command.trace_id,
            },
        ) as span:
            state = self._run(command)
            duration = (
                (state.completed_at or state.updated_at) - state.started_at
            ).total_seconds()
            get_metrics().record_agent_run(
                workflow="agent_runner",
                status=state.status.value,
                termination_reason=(
                    state.termination_reason.value
                    if state.termination_reason is not None
                    else None
                ),
                failure_domain=_agent_failure_domain(state),
                duration=duration,
                steps=state.step_count,
                input_tokens=state.model_input_tokens,
                output_tokens=state.model_output_tokens,
            )
            set_safe_attributes(
                span,
                {
                    "resolveflow.agent.run_id": state.run_id,
                    "resolveflow.agent.status": state.status.value,
                    "resolveflow.agent.termination_reason": (
                        state.termination_reason.value
                        if state.termination_reason is not None
                        else None
                    ),
                    "resolveflow.agent.error_code": (
                        state.terminal_error_code
                    ),
                    "resolveflow.agent.step_count": state.step_count,
                    "gen_ai.usage.input_tokens": state.model_input_tokens,
                    "gen_ai.usage.output_tokens": state.model_output_tokens,
                    "resolveflow.agent.total_tokens": (
                        state.model_input_tokens + state.model_output_tokens
                    ),
                    "resolveflow.agent.final_outcome": state.final_outcome,
                },
            )
            for step in state.steps:
                add_safe_event(
                    span,
                    "agent.state_transition",
                    {
                        "step_number": step.step_number,
                        "action": step.decision.action.value,
                        "tool_name": step.decision.tool_name,
                        "tool_version": step.decision.tool_version,
                        "model_call_id": step.model_call_id,
                        "tool_call_id": step.tool_call_id,
                        "tool_status": step.tool_status,
                        "tool_error_kind": step.tool_error_kind,
                        "verification_code": step.verification_code,
                    },
                )
            if state.status is AgentRunStatus.FAILED:
                mark_span_error(
                    span,
                    FailureDomain.AGENT,
                    state.terminal_error_code
                    or (
                        state.termination_reason.value
                        if state.termination_reason is not None
                        else "agent_failed"
                    ),
                )
            elif state.status is AgentRunStatus.ESCALATED:
                add_safe_event(
                    span,
                    "agent.human_escalation",
                    {
                        "reason": (
                            state.termination_reason.value
                            if state.termination_reason is not None
                            else None
                        ),
                        "error_code": state.terminal_error_code,
                    },
                )
            return state

    def _run(self, command: AgentRunCommand) -> AgentRunState:
        started_at = self._wall_clock()
        started_tick = self._monotonic_clock()
        state = AgentRunState(
            run_id=uuid4(),
            tenant_id=command.actor.tenant_id,
            actor_id=command.actor.actor_id,
            ticket_id=command.ticket_id,
            order_id=command.order_id,
            category=command.category,
            goal=command.goal,
            request_id=command.request_id,
            trace_id=command.trace_id,
            max_steps=self._budget.max_steps,
            max_duration_seconds=self._budget.max_duration_seconds,
            max_total_tokens=self._budget.max_total_tokens,
            max_same_decision_attempts=(
                self._budget.max_same_decision_attempts
            ),
            status=AgentRunStatus.RUNNING,
            termination_reason=None,
            terminal_error_code=None,
            step_count=0,
            model_input_tokens=0,
            model_output_tokens=0,
            observations=(),
            steps=(),
            verification_failures=(),
            final_outcome=None,
            final_summary=None,
            started_at=started_at,
            updated_at=started_at,
            completed_at=None,
        )
        decision_counts: dict[str, int] = {}

        while state.status is AgentRunStatus.RUNNING:
            limit = self._pre_step_limit(state, started_tick)
            if limit is not None:
                return self._terminate(
                    state,
                    status=AgentRunStatus.FAILED,
                    reason=limit,
                )

            allowed_tools = self._registry.descriptors_for(
                command.actor
            )
            try:
                planner_result = self._planner.decide(
                    state,
                    allowed_tools,
                )
            except AgentPlannerError as exc:
                return self._terminate(
                    state,
                    status=AgentRunStatus.FAILED,
                    reason=AgentTerminationReason.PLANNER_ERROR,
                    error_code=exc.error_code,
                )
            except Exception:
                return self._terminate(
                    state,
                    status=AgentRunStatus.FAILED,
                    reason=AgentTerminationReason.PLANNER_ERROR,
                    error_code="planner_unclassified_error",
                )

            decision = planner_result.decision
            step_number = state.step_count + 1
            state = replace(
                state,
                step_count=step_number,
                model_input_tokens=(
                    state.model_input_tokens
                    + planner_result.input_tokens
                ),
                model_output_tokens=(
                    state.model_output_tokens
                    + planner_result.output_tokens
                ),
                updated_at=self._wall_clock(),
            )
            if (
                state.model_input_tokens + state.model_output_tokens
                > self._budget.max_total_tokens
            ):
                state = self._append_step(
                    state,
                    AgentStepTrace(
                        step_number=step_number,
                        decision=decision,
                        model_call_id=planner_result.model_call_id,
                        planner_input_tokens=planner_result.input_tokens,
                        planner_output_tokens=planner_result.output_tokens,
                        verification_code="token_budget_exceeded",
                    ),
                )
                return self._terminate(
                    state,
                    status=AgentRunStatus.FAILED,
                    reason=AgentTerminationReason.TOKEN_BUDGET_EXCEEDED,
                )

            signature = _decision_signature(decision)
            decision_counts[signature] = (
                decision_counts.get(signature, 0) + 1
            )
            if (
                decision_counts[signature]
                > self._budget.max_same_decision_attempts
            ):
                state = self._append_step(
                    state,
                    AgentStepTrace(
                        step_number=step_number,
                        decision=decision,
                        model_call_id=planner_result.model_call_id,
                        planner_input_tokens=planner_result.input_tokens,
                        planner_output_tokens=planner_result.output_tokens,
                        verification_code="repeated_decision_detected",
                    ),
                )
                return self._terminate(
                    state,
                    status=AgentRunStatus.FAILED,
                    reason=AgentTerminationReason.LOOP_DETECTED,
                )

            security = self._decision_security_policy.verify(
                state,
                decision,
                allowed_tools,
            )
            if not security.allowed:
                state = self._append_step(
                    state,
                    AgentStepTrace(
                        step_number=step_number,
                        decision=decision,
                        model_call_id=planner_result.model_call_id,
                        planner_input_tokens=planner_result.input_tokens,
                        planner_output_tokens=planner_result.output_tokens,
                        verification_code=security.code,
                    ),
                )
                state = replace(
                    state,
                    verification_failures=(
                        *state.verification_failures,
                        security.code,
                    ),
                )
                continue

            if decision.action is AgentActionKind.CALL_TOOL:
                state = self._execute_tool(
                    state,
                    command,
                    decision,
                    planner_result,
                )
                if state.status is not AgentRunStatus.RUNNING:
                    return state
                terminal = _tool_terminal_reason(state.observations[-1])
                if terminal is not None:
                    status, reason, error_code = terminal
                    return self._terminate(
                        state,
                        status=status,
                        reason=reason,
                        error_code=error_code,
                    )
                continue

            if decision.action is AgentActionKind.ESCALATE:
                state = self._append_step(
                    state,
                    AgentStepTrace(
                        step_number=step_number,
                        decision=decision,
                        model_call_id=planner_result.model_call_id,
                        planner_input_tokens=planner_result.input_tokens,
                        planner_output_tokens=planner_result.output_tokens,
                        verification_code="planner_requested_escalation",
                    ),
                )
                return self._terminate(
                    state,
                    status=AgentRunStatus.ESCALATED,
                    reason=AgentTerminationReason.HUMAN_ESCALATION,
                )

            if decision.action is AgentActionKind.PROPOSE_REFUND:
                state = self._append_step(
                    state,
                    AgentStepTrace(
                        step_number=step_number,
                        decision=decision,
                        model_call_id=planner_result.model_call_id,
                        planner_input_tokens=planner_result.input_tokens,
                        planner_output_tokens=planner_result.output_tokens,
                        verification_code=(
                            "durable_approval_workflow_required"
                        ),
                    ),
                )
                return self._terminate(
                    state,
                    status=AgentRunStatus.ESCALATED,
                    reason=AgentTerminationReason.HUMAN_ESCALATION,
                    error_code="durable_approval_workflow_required",
                )

            verification = self._verifier.verify(state, decision)
            state = self._append_step(
                state,
                AgentStepTrace(
                    step_number=step_number,
                    decision=decision,
                    model_call_id=planner_result.model_call_id,
                    planner_input_tokens=planner_result.input_tokens,
                    planner_output_tokens=planner_result.output_tokens,
                    verification_code=verification.code,
                ),
            )
            if verification.accepted:
                state = replace(
                    state,
                    final_outcome=decision.outcome,
                    final_summary=decision.final_summary,
                )
                return self._terminate(
                    state,
                    status=AgentRunStatus.COMPLETED,
                    reason=AgentTerminationReason.COMPLETED,
                )
            state = replace(
                state,
                verification_failures=(
                    *state.verification_failures,
                    verification.code,
                ),
            )

        return state

    def _execute_tool(
        self,
        state,
        command,
        decision,
        planner_result,
    ):
        assert decision.tool_name is not None
        assert decision.tool_version is not None
        assert decision.arguments is not None
        try:
            observation = self._tool_executor.execute(
                ToolCallRequest(
                    tool_name=decision.tool_name,
                    tool_version=decision.tool_version,
                    arguments=decision.arguments.as_tool_arguments(),
                    context=ToolExecutionContext(
                        actor=command.actor,
                        request_id=command.request_id,
                        trace_id=command.trace_id,
                        agent_run_id=str(state.run_id),
                        agent_step_id=f"step-{state.step_count}",
                    ),
                )
            )
        except ToolCallRecordingError:
            return self._terminate(
                state,
                status=AgentRunStatus.FAILED,
                reason=AgentTerminationReason.TOOL_RUNTIME_ERROR,
                error_code="tool_call_recording_failed",
            )
        return self._append_step(
            replace(
                state,
                observations=(*state.observations, observation),
            ),
            AgentStepTrace(
                step_number=state.step_count,
                decision=decision,
                model_call_id=planner_result.model_call_id,
                planner_input_tokens=planner_result.input_tokens,
                planner_output_tokens=planner_result.output_tokens,
                tool_call_id=observation.call_id,
                tool_status=observation.status.value,
                tool_error_kind=(
                    observation.error.kind.value
                    if observation.error is not None
                    else None
                ),
            ),
        )

    def _pre_step_limit(
        self,
        state: AgentRunState,
        started_tick: float,
    ) -> AgentTerminationReason | None:
        if state.step_count >= self._budget.max_steps:
            return AgentTerminationReason.MAX_STEPS_EXCEEDED
        if (
            self._monotonic_clock() - started_tick
            >= self._budget.max_duration_seconds
        ):
            return AgentTerminationReason.TIME_BUDGET_EXCEEDED
        return None

    def _append_step(
        self,
        state: AgentRunState,
        step: AgentStepTrace,
    ) -> AgentRunState:
        return replace(
            state,
            steps=(*state.steps, step),
            updated_at=self._wall_clock(),
        )

    def _terminate(
        self,
        state: AgentRunState,
        *,
        status: AgentRunStatus,
        reason: AgentTerminationReason,
        error_code: str | None = None,
    ) -> AgentRunState:
        completed_at = self._wall_clock()
        return replace(
            state,
            status=status,
            termination_reason=reason,
            terminal_error_code=error_code,
            updated_at=completed_at,
            completed_at=completed_at,
        )


def _decision_signature(decision) -> str:
    document = decision.model_dump(mode="json", exclude_none=True)
    serialized = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(serialized).hexdigest()


def _tool_terminal_reason(observation):
    if observation.succeeded or observation.error is None:
        return None
    if observation.error.kind is ToolFailureKind.AUTHORIZATION:
        return (
            AgentRunStatus.ESCALATED,
            AgentTerminationReason.TOOL_PERMISSION_DENIED,
            observation.error.code,
        )
    if observation.error.kind in {
        ToolFailureKind.OUTPUT_CONTRACT,
        ToolFailureKind.INTERNAL,
    }:
        return (
            AgentRunStatus.FAILED,
            AgentTerminationReason.TOOL_CONTRACT_ERROR,
            observation.error.code,
        )
    return None


def _agent_failure_domain(state: AgentRunState) -> str:
    if state.status is AgentRunStatus.COMPLETED:
        return "none"
    reason = state.termination_reason
    if reason in {
        AgentTerminationReason.TOOL_RUNTIME_ERROR,
        AgentTerminationReason.TOOL_PERMISSION_DENIED,
        AgentTerminationReason.TOOL_CONTRACT_ERROR,
    }:
        return "tool"
    if reason is AgentTerminationReason.PLANNER_ERROR:
        return "model"
    return "agent"
