from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
from typing import Any, TypedDict
from uuid import UUID, uuid4

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.agent.errors import (
    AgentCheckpointCompatibilityError,
    AgentPlannerError,
    AgentRunAlreadyExistsError,
    AgentRunNotFoundError,
    RefundApprovalRequiredError,
    RefundReviewDeniedError,
)
from app.agent.approval import (
    RefundApprovalRecord,
    RefundReviewAction,
    RefundReviewCommand,
)
from app.agent.models import (
    AgentActionKind,
    AgentBudget,
    AgentDecision,
    AgentRunCommand,
    AgentRunState,
    AgentRunStatus,
    AgentStepTrace,
    AgentTerminationReason,
    InvestigationOutcome,
)
from app.agent.planner import AGENT_PROMPT, AgentPlanner
from app.agent.security import AgentDecisionSecurityPolicy
from app.agent.verifier import InvestigationCompletionVerifier
from app.domain.auth import AuthenticatedActor, Permission, Role
from app.domain.ticket import TicketCategory
from app.policy.refund import (
    RefundPolicyEngine,
    RefundProposal,
    RefundProposalStatus,
)
from app.tools.contracts import (
    ToolCallRequest,
    ToolCallStatus,
    ToolErrorDetail,
    ToolExecutionContext,
    ToolFailureKind,
    ToolObservation,
    ToolRiskLevel,
    ToolSideEffect,
)
from app.tools.errors import ToolCallRecordingError, ToolNotFoundError
from app.tools.executor import ToolExecutor
from app.tools.refund import (
    RefundExecutionGuard,
    RefundExecutionStatus,
    RefundObservation,
)
from app.tools.registry import ToolRegistry
from app.observability.tracing import (
    FailureDomain,
    add_safe_event,
    mark_span_error,
    operation_span,
    set_safe_attributes,
)


class AgentGraphState(TypedDict):
    schema_version: int
    run_id: str
    tenant_id: str
    actor_id: str
    actor_roles: list[str]
    ticket_id: str
    order_id: str
    category: str
    goal: str
    request_id: str
    trace_id: str
    max_steps: int
    max_duration_seconds: float
    max_total_tokens: int
    max_same_decision_attempts: int
    status: str
    termination_reason: str | None
    terminal_error_code: str | None
    step_count: int
    model_input_tokens: int
    model_output_tokens: int
    observations: list[dict[str, Any]]
    steps: list[dict[str, Any]]
    verification_failures: list[str]
    final_outcome: str | None
    final_summary: str | None
    started_at: str
    updated_at: str
    completed_at: str | None
    current_node: str
    decision_counts: dict[str, int]
    pending_planner: dict[str, Any] | None
    pause_requested: bool
    cancel_requested: bool
    pause_after_step: int | None
    paused_at: str | None
    paused_duration_seconds: float
    refund_proposal: dict[str, Any] | None
    refund_proposal_history: list[dict[str, Any]]
    refund_approval: dict[str, Any] | None
    refund_audit_events: list[dict[str, Any]]
    refund_last_modifier_actor_id: str | None


@dataclass(frozen=True, slots=True)
class DurableAgentRunView:
    state: AgentRunState
    paused: bool
    next_nodes: tuple[str, ...]
    interrupt_payloads: tuple[Any, ...]
    checkpoint_id: str | None
    refund_proposal: RefundProposal | None
    refund_proposal_history: tuple[RefundProposal, ...]
    refund_approval: RefundApprovalRecord | None
    refund_audit_events: tuple[dict[str, Any], ...]


class DurableAgentWorkflow:
    """LangGraph mapping of the Day8 loop with durable checkpoints."""

    STATE_SCHEMA_VERSION = 1

    def __init__(
        self,
        planner: AgentPlanner,
        registry: ToolRegistry,
        tool_executor: ToolExecutor,
        checkpointer: BaseCheckpointSaver,
        verifier: InvestigationCompletionVerifier | None = None,
        *,
        budget: AgentBudget | None = None,
        wall_clock: Callable[[], datetime] | None = None,
        refund_policy_engine: RefundPolicyEngine | None = None,
        refund_execution_guard: RefundExecutionGuard | None = None,
        decision_security_policy: AgentDecisionSecurityPolicy | None = None,
    ) -> None:
        self._planner = planner
        self._registry = registry
        self._tool_executor = tool_executor
        self._verifier = verifier or InvestigationCompletionVerifier()
        self._budget = budget or AgentBudget()
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))
        self._refund_policy_engine = (
            refund_policy_engine or RefundPolicyEngine()
        )
        self._refund_execution_guard = refund_execution_guard
        self._decision_security_policy = (
            decision_security_policy
            or AgentDecisionSecurityPolicy(
                protected_values=(AGENT_PROMPT.instructions,)
            )
        )
        self._graph = self._build_graph(checkpointer)

    def start(
        self,
        command: AgentRunCommand,
        *,
        run_id: UUID | None = None,
        pause_after_step: int | None = None,
    ) -> DurableAgentRunView:
        with operation_span(
            "start durable after-sales agent",
            failure_domain=FailureDomain.AGENT,
            attributes={
                "resolveflow.component": "agent",
                "resolveflow.agent.workflow": "langgraph",
                "resolveflow.request_id": command.request_id,
                "resolveflow.trace_id": command.trace_id,
                "resolveflow.agent.pause_after_step": pause_after_step,
            },
        ) as span:
            view = self._start(
                command,
                run_id=run_id,
                pause_after_step=pause_after_step,
            )
            self._annotate_view(span, view)
            return view

    def _start(
        self,
        command: AgentRunCommand,
        *,
        run_id: UUID | None = None,
        pause_after_step: int | None = None,
    ) -> DurableAgentRunView:
        resolved_run_id = run_id or uuid4()
        config = self._config(resolved_run_id)
        existing = self._graph.get_state(config)
        if existing.values:
            raise AgentRunAlreadyExistsError(str(resolved_run_id))
        if pause_after_step is not None and pause_after_step < 1:
            raise ValueError("pause_after_step must be positive.")
        initial = self._initial_state(
            command,
            resolved_run_id,
            pause_after_step=pause_after_step,
        )
        self._graph.invoke(initial, config=config)
        return self.get(resolved_run_id)

    def resume(
        self,
        run_id: UUID,
        *,
        action: str = "continue",
    ) -> DurableAgentRunView:
        with operation_span(
            "resume durable after-sales agent",
            failure_domain=FailureDomain.AGENT,
            attributes={
                "resolveflow.component": "agent",
                "resolveflow.agent.workflow": "langgraph",
                "resolveflow.agent.run_id": run_id,
                "resolveflow.agent.resume_action": action,
            },
        ) as span:
            view = self._resume(run_id, action=action)
            self._annotate_view(span, view)
            return view

    def _resume(
        self,
        run_id: UUID,
        *,
        action: str = "continue",
    ) -> DurableAgentRunView:
        snapshot = self._require_snapshot(run_id)
        if (
            _refund_interrupt_payload(snapshot) is not None
            and action != "cancel"
        ):
            raise RefundApprovalRequiredError(
                "Use review_refund with a trusted reviewer identity."
            )
        if not _snapshot_is_paused(snapshot):
            if not snapshot.next:
                return self._view(snapshot)
            self._graph.invoke(None, config=self._config(run_id))
            return self.get(run_id)
        self._graph.invoke(
            Command(resume={"action": action}),
            config=self._config(run_id),
        )
        return self.get(run_id)

    def review_refund(
        self,
        run_id: UUID,
        command: RefundReviewCommand,
    ) -> DurableAgentRunView:
        with operation_span(
            "review refund proposal",
            failure_domain=FailureDomain.POLICY,
            attributes={
                "resolveflow.component": "human_approval",
                "resolveflow.agent.run_id": run_id,
                "resolveflow.approval.action": command.action.value,
                "resolveflow.approval.expected_version": (
                    command.expected_proposal_version
                ),
                "resolveflow.approval.proposal_hash": (
                    command.expected_proposal_hash
                ),
            },
        ) as span:
            try:
                view = self._review_refund(run_id, command)
            except RefundReviewDeniedError as exc:
                mark_span_error(
                    span,
                    FailureDomain.POLICY,
                    str(exc),
                    retryable=False,
                )
                raise
            self._annotate_view(span, view)
            add_safe_event(
                span,
                "approval.reviewed",
                {"action": command.action.value},
            )
            return view

    def _review_refund(
        self,
        run_id: UUID,
        command: RefundReviewCommand,
    ) -> DurableAgentRunView:
        snapshot = self._require_snapshot(run_id)
        interrupt_payload = _refund_interrupt_payload(snapshot)
        if interrupt_payload is None:
            raise RefundApprovalRequiredError(
                "The run is not waiting at a refund approval gate."
            )
        values = dict(snapshot.values)
        proposal_document = values.get("refund_proposal")
        if proposal_document is None:
            raise AgentCheckpointCompatibilityError(
                "Refund approval checkpoint has no proposal."
            )
        proposal = RefundProposal.model_validate(proposal_document)
        if self._now() >= proposal.expires_at:
            self._graph.invoke(
                Command(resume={"action": "expire"}),
                config=self._config(run_id),
            )
            return self.get(run_id)
        if command.actor.tenant_id != proposal.tenant_id:
            raise RefundReviewDeniedError("refund_review_cross_tenant")
        if not command.actor.has_permission(Permission.REFUND_APPROVE):
            raise RefundReviewDeniedError(
                "refund_review_permission_denied"
            )
        if command.actor.actor_id == proposal.requester_actor_id:
            raise RefundReviewDeniedError(
                "refund_review_four_eyes_required"
            )
        if (
            command.action is RefundReviewAction.APPROVE
            and command.actor.actor_id
            == values.get("refund_last_modifier_actor_id")
        ):
            raise RefundReviewDeniedError(
                "refund_review_modifier_cannot_approve"
            )
        if (
            command.expected_proposal_version != proposal.version
            or command.expected_proposal_hash != proposal.proposal_hash
        ):
            raise RefundReviewDeniedError(
                "refund_review_binding_mismatch"
            )
        self._graph.invoke(
            Command(
                resume={
                    "action": command.action.value,
                    "reviewer": {
                        "actor_id": command.actor.actor_id,
                        "tenant_id": str(command.actor.tenant_id),
                        "roles": sorted(
                            role.value for role in command.actor.roles
                        ),
                    },
                    "expected_proposal_version": (
                        command.expected_proposal_version
                    ),
                    "expected_proposal_hash": (
                        command.expected_proposal_hash
                    ),
                    "reason": command.reason,
                    "modified_proposal": (
                        command.modified_proposal.model_dump(mode="json")
                        if command.modified_proposal is not None
                        else None
                    ),
                }
            ),
            config=self._config(run_id),
        )
        return self.get(run_id)

    def request_pause(self, run_id: UUID) -> DurableAgentRunView:
        snapshot = self._require_snapshot(run_id)
        if _snapshot_is_paused(snapshot) or not snapshot.next:
            return self._view(snapshot)
        paused_at = self._now().isoformat()
        self._graph.update_state(
            self._config(run_id),
            {
                "pause_requested": True,
                "paused_at": paused_at,
                "updated_at": paused_at,
            },
        )
        self._graph.invoke(None, config=self._config(run_id))
        return self.get(run_id)

    def cancel(self, run_id: UUID) -> DurableAgentRunView:
        snapshot = self._require_snapshot(run_id)
        if not snapshot.next:
            return self._view(snapshot)
        if _snapshot_is_paused(snapshot):
            return self.resume(run_id, action="cancel")
        self._graph.update_state(
            self._config(run_id),
            {"cancel_requested": True},
        )
        self._graph.invoke(None, config=self._config(run_id))
        return self.get(run_id)

    def get(self, run_id: UUID) -> DurableAgentRunView:
        return self._view(self._require_snapshot(run_id))

    def checkpoint_count(self, run_id: UUID) -> int:
        self._require_snapshot(run_id)
        return sum(
            1
            for _ in self._graph.get_state_history(
                self._config(run_id)
            )
        )

    def _build_graph(self, checkpointer):
        builder = StateGraph(AgentGraphState)
        builder.add_node("control", self._control_node)
        builder.add_node("plan", self._traced_node("plan", self._plan_node))
        builder.add_node(
            "execute_tool",
            self._traced_node("execute_tool", self._execute_tool_node),
        )
        builder.add_node("verify", self._traced_node("verify", self._verify_node))
        builder.add_node(
            "evaluate_refund",
            self._traced_node(
                "evaluate_refund",
                self._evaluate_refund_node,
            ),
        )
        builder.add_node("refund_approval", self._refund_approval_node)
        builder.add_node(
            "execute_refund",
            self._traced_node("execute_refund", self._execute_refund_node),
        )
        builder.add_node(
            "verify_refund",
            self._traced_node("verify_refund", self._verify_refund_node),
        )
        builder.add_edge(START, "control")
        builder.add_conditional_edges(
            "control",
            self._route_after_control,
            {"plan": "plan", "end": END},
        )
        builder.add_conditional_edges(
            "plan",
            self._route_after_plan,
            {
                "control": "control",
                "execute_tool": "execute_tool",
                "verify": "verify",
                "evaluate_refund": "evaluate_refund",
                "end": END,
            },
        )
        builder.add_edge("execute_tool", "control")
        builder.add_conditional_edges(
            "verify",
            self._route_after_verify,
            {"control": "control", "end": END},
        )
        builder.add_conditional_edges(
            "evaluate_refund",
            self._route_after_refund_evaluation,
            {"refund_approval": "refund_approval", "end": END},
        )
        builder.add_conditional_edges(
            "refund_approval",
            self._route_after_refund_approval,
            {
                "refund_approval": "refund_approval",
                "execute_refund": "execute_refund",
                "end": END,
            },
        )
        builder.add_conditional_edges(
            "execute_refund",
            self._route_after_refund_execution,
            {"verify_refund": "verify_refund", "end": END},
        )
        builder.add_edge("verify_refund", END)
        return builder.compile(
            checkpointer=checkpointer,
            name="resolveflow_after_sales_agent",
        )

    @staticmethod
    def _traced_node(name: str, handler):
        def invoke(state: AgentGraphState):
            with operation_span(
                f"agent node {name}",
                failure_domain=FailureDomain.AGENT,
                attributes={
                    "resolveflow.component": "agent_node",
                    "resolveflow.agent.node": name,
                    "resolveflow.agent.run_id": state.get("run_id"),
                    "resolveflow.agent.step_count": state.get("step_count"),
                    "resolveflow.request_id": state.get("request_id"),
                    "resolveflow.trace_id": state.get("trace_id"),
                },
            ) as span:
                result = handler(state)
                if isinstance(result, dict):
                    set_safe_attributes(
                        span,
                        {
                            "resolveflow.agent.next_status": result.get(
                                "status"
                            ),
                            "resolveflow.agent.next_node": result.get(
                                "current_node"
                            ),
                            "resolveflow.agent.error_code": result.get(
                                "terminal_error_code"
                            ),
                        },
                    )
                return result

        invoke.__name__ = f"traced_{name}_node"
        return invoke

    @staticmethod
    def _annotate_view(span, view: DurableAgentRunView) -> None:
        state = view.state
        set_safe_attributes(
            span,
            {
                "resolveflow.agent.run_id": state.run_id,
                "resolveflow.agent.status": state.status.value,
                "resolveflow.agent.step_count": state.step_count,
                "resolveflow.agent.paused": view.paused,
                "resolveflow.agent.next_nodes": view.next_nodes,
                "resolveflow.agent.termination_reason": (
                    state.termination_reason.value
                    if state.termination_reason is not None
                    else None
                ),
                "resolveflow.agent.error_code": state.terminal_error_code,
                "gen_ai.usage.input_tokens": state.model_input_tokens,
                "gen_ai.usage.output_tokens": state.model_output_tokens,
            },
        )
        if state.status is AgentRunStatus.FAILED:
            mark_span_error(
                span,
                FailureDomain.AGENT,
                state.terminal_error_code or "agent_failed",
            )

    def _control_node(
        self,
        state: AgentGraphState,
    ) -> dict[str, Any]:
        if state["status"] != AgentRunStatus.RUNNING.value:
            return {"current_node": "control"}
        if state["cancel_requested"]:
            return self._terminal_update(
                AgentRunStatus.CANCELLED,
                AgentTerminationReason.CANCELLED,
                current_node="control",
            )
        if state["pause_requested"]:
            response = interrupt(
                {
                    "kind": "agent_run_pause",
                    "run_id": state["run_id"],
                    "step_count": state["step_count"],
                    "last_tool": _last_tool_name(state),
                    "allowed_actions": ["continue", "cancel"],
                }
            )
            action = (
                response.get("action")
                if isinstance(response, dict)
                else None
            )
            if action == "cancel":
                return self._terminal_update(
                    AgentRunStatus.CANCELLED,
                    AgentTerminationReason.CANCELLED,
                    current_node="control",
                )
            if action != "continue":
                return self._terminal_update(
                    AgentRunStatus.FAILED,
                    AgentTerminationReason.PLANNER_ERROR,
                    error_code="invalid_resume_action",
                    current_node="control",
                )
            paused_at = (
                _parse_datetime(state["paused_at"])
                if state["paused_at"] is not None
                else self._now()
            )
            now = self._now()
            return {
                "pause_requested": False,
                "paused_at": None,
                "paused_duration_seconds": (
                    state["paused_duration_seconds"]
                    + max(0.0, (now - paused_at).total_seconds())
                ),
                "current_node": "control",
                "updated_at": now.isoformat(),
            }
        return {
            "current_node": "control",
            "updated_at": self._now().isoformat(),
        }

    def _plan_node(
        self,
        state: AgentGraphState,
    ) -> dict[str, Any]:
        if state["status"] != AgentRunStatus.RUNNING.value:
            return {"current_node": "plan"}
        limit = self._pre_step_limit(state)
        if limit is not None:
            return self._terminal_update(
                AgentRunStatus.FAILED,
                limit,
                current_node="plan",
            )

        actor = _actor_from_state(state)
        runtime_state = self._runtime_state(state)
        try:
            planner_result = self._planner.decide(
                runtime_state,
                self._registry.descriptors_for(actor),
            )
        except AgentPlannerError as exc:
            return self._terminal_update(
                AgentRunStatus.FAILED,
                AgentTerminationReason.PLANNER_ERROR,
                error_code=exc.error_code,
                current_node="plan",
            )
        except Exception:
            return self._terminal_update(
                AgentRunStatus.FAILED,
                AgentTerminationReason.PLANNER_ERROR,
                error_code="planner_unclassified_error",
                current_node="plan",
            )

        decision = planner_result.decision
        step_number = state["step_count"] + 1
        input_tokens = (
            state["model_input_tokens"]
            + planner_result.input_tokens
        )
        output_tokens = (
            state["model_output_tokens"]
            + planner_result.output_tokens
        )
        pending = {
            "decision": decision.model_dump(mode="json"),
            "model_call_id": (
                str(planner_result.model_call_id)
                if planner_result.model_call_id is not None
                else None
            ),
            "input_tokens": planner_result.input_tokens,
            "output_tokens": planner_result.output_tokens,
        }
        common: dict[str, Any] = {
            "step_count": step_number,
            "model_input_tokens": input_tokens,
            "model_output_tokens": output_tokens,
            "pending_planner": pending,
            "current_node": "plan",
            "updated_at": self._now().isoformat(),
        }
        if input_tokens + output_tokens > state["max_total_tokens"]:
            trace = _pending_trace(
                step_number,
                pending,
                verification_code="token_budget_exceeded",
            )
            return {
                **common,
                "steps": [*state["steps"], trace],
                **self._terminal_update(
                    AgentRunStatus.FAILED,
                    AgentTerminationReason.TOKEN_BUDGET_EXCEEDED,
                    current_node="plan",
                ),
            }

        signature = _decision_signature(decision)
        decision_counts = dict(state["decision_counts"])
        decision_counts[signature] = (
            decision_counts.get(signature, 0) + 1
        )
        common["decision_counts"] = decision_counts
        if (
            decision_counts[signature]
            > state["max_same_decision_attempts"]
        ):
            trace = _pending_trace(
                step_number,
                pending,
                verification_code="repeated_decision_detected",
            )
            return {
                **common,
                "steps": [*state["steps"], trace],
                **self._terminal_update(
                    AgentRunStatus.FAILED,
                    AgentTerminationReason.LOOP_DETECTED,
                    current_node="plan",
                ),
            }

        security = self._decision_security_policy.verify(
            runtime_state,
            decision,
            self._registry.descriptors_for(actor),
        )
        if not security.allowed:
            trace = _pending_trace(
                step_number,
                pending,
                verification_code=security.code,
            )
            return {
                **common,
                "pending_planner": None,
                "steps": [*state["steps"], trace],
                "verification_failures": [
                    *state["verification_failures"],
                    security.code,
                ],
            }

        if decision.action is AgentActionKind.ESCALATE:
            trace = _pending_trace(
                step_number,
                pending,
                verification_code="planner_requested_escalation",
            )
            return {
                **common,
                "steps": [*state["steps"], trace],
                **self._terminal_update(
                    AgentRunStatus.ESCALATED,
                    AgentTerminationReason.HUMAN_ESCALATION,
                    current_node="plan",
                ),
            }
        return common

    def _execute_tool_node(
        self,
        state: AgentGraphState,
    ) -> dict[str, Any]:
        pending = _require_pending(state)
        decision = AgentDecision.model_validate(pending["decision"])
        if decision.action is not AgentActionKind.CALL_TOOL:
            return self._terminal_update(
                AgentRunStatus.FAILED,
                AgentTerminationReason.TOOL_RUNTIME_ERROR,
                error_code="checkpoint_action_mismatch",
                current_node="execute_tool",
            )
        assert decision.tool_name is not None
        assert decision.tool_version is not None
        assert decision.arguments is not None
        try:
            observation = self._tool_executor.execute(
                ToolCallRequest(
                    tool_name=decision.tool_name,
                    tool_version=decision.tool_version,
                    arguments=(
                        decision.arguments.as_tool_arguments()
                    ),
                    context=ToolExecutionContext(
                        actor=_actor_from_state(state),
                        request_id=state["request_id"],
                        trace_id=state["trace_id"],
                        agent_run_id=state["run_id"],
                        agent_step_id=f"step-{state['step_count']}",
                    ),
                )
            )
        except ToolCallRecordingError:
            return self._terminal_update(
                AgentRunStatus.FAILED,
                AgentTerminationReason.TOOL_RUNTIME_ERROR,
                error_code="tool_call_recording_failed",
                current_node="execute_tool",
            )

        encoded = _encode_observation(observation)
        trace = _pending_trace(
            state["step_count"],
            pending,
            tool_call_id=str(observation.call_id),
            tool_status=observation.status.value,
            tool_error_kind=(
                observation.error.kind.value
                if observation.error is not None
                else None
            ),
        )
        update: dict[str, Any] = {
            "observations": [*state["observations"], encoded],
            "steps": [*state["steps"], trace],
            "pending_planner": None,
            "current_node": "execute_tool",
            "updated_at": self._now().isoformat(),
        }
        terminal = _tool_terminal_reason(observation)
        if terminal is not None:
            status, reason, error_code = terminal
            return {
                **update,
                **self._terminal_update(
                    status,
                    reason,
                    error_code=error_code,
                    current_node="execute_tool",
                ),
            }
        if (
            state["pause_after_step"] is not None
            and state["step_count"] >= state["pause_after_step"]
        ):
            paused_at = self._now()
            update["pause_requested"] = True
            update["pause_after_step"] = None
            update["paused_at"] = paused_at.isoformat()
            update["updated_at"] = paused_at.isoformat()
        return update

    def _evaluate_refund_node(
        self,
        state: AgentGraphState,
    ) -> dict[str, Any]:
        pending = _require_pending(state)
        decision = AgentDecision.model_validate(pending["decision"])
        if (
            decision.action is not AgentActionKind.PROPOSE_REFUND
            or decision.refund_proposal is None
        ):
            return self._terminal_update(
                AgentRunStatus.FAILED,
                AgentTerminationReason.PLANNER_ERROR,
                error_code="checkpoint_action_mismatch",
                current_node="evaluate_refund",
            )
        evaluation = self._refund_policy_engine.evaluate(
            self._runtime_state(state),
            decision.refund_proposal,
            now=self._now(),
        )
        trace = _pending_trace(
            state["step_count"],
            pending,
            verification_code=evaluation.code,
        )
        update: dict[str, Any] = {
            "steps": [*state["steps"], trace],
            "pending_planner": None,
            "current_node": "evaluate_refund",
            "updated_at": self._now().isoformat(),
            "refund_audit_events": [
                *state.get("refund_audit_events", []),
                _refund_audit_event(
                    event_type="refund_policy_evaluated",
                    occurred_at=self._now(),
                    actor_id=state["actor_id"],
                    details={
                        "allowed": evaluation.allowed,
                        "code": evaluation.code,
                        "missing_evidence": list(
                            evaluation.missing_evidence
                        ),
                    },
                ),
            ],
        }
        if not evaluation.allowed or evaluation.proposal is None:
            return {
                **update,
                **self._terminal_update(
                    AgentRunStatus.ESCALATED,
                    AgentTerminationReason.POLICY_DENIED,
                    error_code=evaluation.code,
                    current_node="evaluate_refund",
                ),
            }
        proposal_document = evaluation.proposal.model_dump(mode="json")
        update["refund_proposal"] = proposal_document
        update["refund_proposal_history"] = [proposal_document]
        update["refund_last_modifier_actor_id"] = None
        update["refund_audit_events"] = [
            *update["refund_audit_events"],
            _refund_audit_event(
                event_type="refund_proposal_created",
                occurred_at=self._now(),
                actor_id=state["actor_id"],
                details={
                    "proposal_id": str(evaluation.proposal.proposal_id),
                    "proposal_version": evaluation.proposal.version,
                    "proposal_hash": evaluation.proposal.proposal_hash,
                    "amount_minor": evaluation.proposal.amount_minor,
                    "currency": evaluation.proposal.currency,
                    "policy_id": evaluation.proposal.policy_id,
                    "policy_version": evaluation.proposal.policy_version,
                },
            ),
        ]
        return update

    def _refund_approval_node(
        self,
        state: AgentGraphState,
    ) -> dict[str, Any]:
        proposal_document = state.get("refund_proposal")
        if proposal_document is None:
            return self._terminal_update(
                AgentRunStatus.FAILED,
                AgentTerminationReason.PLANNER_ERROR,
                error_code="refund_proposal_missing",
                current_node="refund_approval",
            )
        proposal = RefundProposal.model_validate(proposal_document)
        response = interrupt(
            {
                "kind": "refund_approval",
                "run_id": state["run_id"],
                "proposal_id": str(proposal.proposal_id),
                "proposal_version": proposal.version,
                "proposal_hash": proposal.proposal_hash,
                "order_id": proposal.order_id,
                "amount_minor": proposal.amount_minor,
                "currency": proposal.currency,
                "reason": proposal.reason,
                "policy_id": proposal.policy_id,
                "policy_version": proposal.policy_version,
                "expires_at": proposal.expires_at.isoformat(),
                "allowed_actions": [
                    item.value for item in RefundReviewAction
                ],
            }
        )
        action_value = (
            response.get("action")
            if isinstance(response, dict)
            else None
        )
        if action_value == "cancel":
            return self._terminal_update(
                AgentRunStatus.CANCELLED,
                AgentTerminationReason.CANCELLED,
                current_node="refund_approval",
            )
        if action_value == "expire" or self._now() >= proposal.expires_at:
            expired = proposal.model_copy(
                update={"status": RefundProposalStatus.EXPIRED}
            )
            return {
                "refund_proposal": expired.model_dump(mode="json"),
                "refund_proposal_history": _replace_refund_proposal(
                    state.get("refund_proposal_history", []),
                    expired,
                ),
                "refund_audit_events": [
                    *state.get("refund_audit_events", []),
                    _refund_audit_event(
                        event_type="refund_approval_expired",
                        occurred_at=self._now(),
                        actor_id="system",
                        details={
                            "proposal_id": str(proposal.proposal_id),
                            "proposal_version": proposal.version,
                        },
                    ),
                ],
                **self._terminal_update(
                    AgentRunStatus.ESCALATED,
                    AgentTerminationReason.APPROVAL_EXPIRED,
                    error_code="refund_approval_expired",
                    current_node="refund_approval",
                ),
            }
        validation = _validate_refund_review_response(
            state,
            proposal,
            response,
        )
        if validation["error_code"] is not None:
            return {
                "refund_audit_events": [
                    *state.get("refund_audit_events", []),
                    _refund_audit_event(
                        event_type="refund_review_rejected_by_backend",
                        occurred_at=self._now(),
                        actor_id=validation["actor_id"] or "unknown",
                        details={
                            "code": validation["error_code"],
                            "proposal_version": proposal.version,
                        },
                    ),
                ],
                **self._terminal_update(
                    AgentRunStatus.ESCALATED,
                    AgentTerminationReason.HUMAN_ESCALATION,
                    error_code=validation["error_code"],
                    current_node="refund_approval",
                ),
            }

        action = RefundReviewAction(action_value)
        reviewer = validation["actor"]
        assert isinstance(reviewer, AuthenticatedActor)
        reason = str(response["reason"])
        if action is RefundReviewAction.MODIFY:
            from app.agent.models import RefundProposalCandidate

            candidate = RefundProposalCandidate.model_validate(
                response.get("modified_proposal")
            )
            evaluation = self._refund_policy_engine.evaluate(
                self._runtime_state(state),
                candidate,
                now=self._now(),
                version=proposal.version + 1,
            )
            superseded = proposal.model_copy(
                update={"status": RefundProposalStatus.SUPERSEDED}
            )
            history = _replace_refund_proposal(
                state.get("refund_proposal_history", []),
                superseded,
            )
            audit_events = [
                *state.get("refund_audit_events", []),
                _refund_audit_event(
                    event_type="refund_proposal_modified",
                    occurred_at=self._now(),
                    actor_id=reviewer.actor_id,
                    details={
                        "old_version": proposal.version,
                        "new_version": proposal.version + 1,
                        "reason": reason,
                        "policy_code": evaluation.code,
                    },
                ),
            ]
            if not evaluation.allowed or evaluation.proposal is None:
                return {
                    "refund_proposal": superseded.model_dump(mode="json"),
                    "refund_proposal_history": history,
                    "refund_audit_events": audit_events,
                    **self._terminal_update(
                        AgentRunStatus.ESCALATED,
                        AgentTerminationReason.POLICY_DENIED,
                        error_code=evaluation.code,
                        current_node="refund_approval",
                    ),
                }
            new_document = evaluation.proposal.model_dump(mode="json")
            return {
                "refund_proposal": new_document,
                "refund_proposal_history": [*history, new_document],
                "refund_approval": None,
                "refund_last_modifier_actor_id": reviewer.actor_id,
                "refund_audit_events": audit_events,
                "current_node": "refund_approval",
                "updated_at": self._now().isoformat(),
            }

        approval = RefundApprovalRecord(
            approval_id=uuid4(),
            proposal_id=proposal.proposal_id,
            proposal_version=proposal.version,
            proposal_hash=proposal.proposal_hash,
            reviewer_actor_id=reviewer.actor_id,
            reviewer_tenant_id=reviewer.tenant_id,
            reviewer_roles=tuple(
                sorted(role.value for role in reviewer.roles)
            ),
            action=action,
            reason=reason,
            reviewed_at=self._now(),
        )
        status = (
            RefundProposalStatus.APPROVED
            if action is RefundReviewAction.APPROVE
            else RefundProposalStatus.REJECTED
        )
        reviewed_proposal = proposal.model_copy(
            update={"status": status}
        )
        base_update: dict[str, Any] = {
            "refund_proposal": reviewed_proposal.model_dump(mode="json"),
            "refund_proposal_history": _replace_refund_proposal(
                state.get("refund_proposal_history", []),
                reviewed_proposal,
            ),
            "refund_approval": approval.model_dump(mode="json"),
            "refund_audit_events": [
                *state.get("refund_audit_events", []),
                _refund_audit_event(
                    event_type=f"refund_{action.value}",
                    occurred_at=approval.reviewed_at,
                    actor_id=reviewer.actor_id,
                    details={
                        "approval_id": str(approval.approval_id),
                        "proposal_version": proposal.version,
                        "proposal_hash": proposal.proposal_hash,
                        "reason": reason,
                    },
                ),
            ],
            "current_node": "refund_approval",
            "updated_at": self._now().isoformat(),
        }
        if action is RefundReviewAction.APPROVE:
            return base_update
        termination_reason = (
            AgentTerminationReason.HUMAN_TAKEOVER
            if action is RefundReviewAction.TAKEOVER
            else AgentTerminationReason.APPROVAL_REJECTED
        )
        return {
            **base_update,
            **self._terminal_update(
                AgentRunStatus.ESCALATED,
                termination_reason,
                error_code=f"refund_{action.value}",
                current_node="refund_approval",
            ),
        }

    def _execute_refund_node(
        self,
        state: AgentGraphState,
    ) -> dict[str, Any]:
        proposal_document = state.get("refund_proposal")
        approval_document = state.get("refund_approval")
        if proposal_document is None or approval_document is None:
            return self._terminal_update(
                AgentRunStatus.FAILED,
                AgentTerminationReason.TOOL_RUNTIME_ERROR,
                error_code="refund_approval_binding_missing",
                current_node="execute_refund",
            )
        proposal = RefundProposal.model_validate(proposal_document)
        approval = RefundApprovalRecord.model_validate(approval_document)
        if (
            proposal.status is not RefundProposalStatus.APPROVED
            or approval.action is not RefundReviewAction.APPROVE
            or approval.proposal_id != proposal.proposal_id
            or approval.proposal_version != proposal.version
            or approval.proposal_hash != proposal.proposal_hash
        ):
            return self._terminal_update(
                AgentRunStatus.FAILED,
                AgentTerminationReason.TOOL_RUNTIME_ERROR,
                error_code="refund_approval_binding_invalid",
                current_node="execute_refund",
            )
        reviewer = _actor_from_approval(approval)
        if self._refund_execution_guard is None:
            return self._terminal_update(
                AgentRunStatus.FAILED,
                AgentTerminationReason.TOOL_RUNTIME_ERROR,
                error_code="refund_execution_guard_not_configured",
                current_node="execute_refund",
            )
        arguments = {
            "order_id": proposal.order_id,
            "amount_minor": proposal.amount_minor,
            "currency": proposal.currency,
            "reason": proposal.reason,
            "proposal_hash": proposal.proposal_hash,
            "approval_id": str(approval.approval_id),
            "idempotency_key": proposal.idempotency_key,
        }
        arguments["execution_grant"] = (
            self._refund_execution_guard.issue(
                arguments,
                tenant_id=proposal.tenant_id,
                actor_id=reviewer.actor_id,
            )
        )
        try:
            observation = self._tool_executor.execute(
                ToolCallRequest(
                    tool_name="refund_execute",
                    tool_version="1.0.0",
                    arguments=arguments,
                    context=ToolExecutionContext(
                        actor=reviewer,
                        request_id=state["request_id"],
                        trace_id=state["trace_id"],
                        agent_run_id=state["run_id"],
                        agent_step_id="refund-execute",
                    ),
                )
            )
        except ToolCallRecordingError:
            return self._terminal_update(
                AgentRunStatus.FAILED,
                AgentTerminationReason.TOOL_RUNTIME_ERROR,
                error_code="refund_tool_call_recording_failed",
                current_node="execute_refund",
            )
        update: dict[str, Any] = {
            "observations": [
                *state["observations"],
                _encode_observation(observation),
            ],
            "refund_audit_events": [
                *state.get("refund_audit_events", []),
                _refund_audit_event(
                    event_type="refund_write_attempted",
                    occurred_at=self._now(),
                    actor_id=reviewer.actor_id,
                    details={
                        "proposal_version": proposal.version,
                        "idempotency_key": proposal.idempotency_key,
                        "tool_call_id": str(observation.call_id),
                        "status": observation.status.value,
                        "error_code": (
                            observation.error.code
                            if observation.error is not None
                            else None
                        ),
                    },
                ),
            ],
            "current_node": "execute_refund",
            "updated_at": self._now().isoformat(),
        }
        if observation.succeeded:
            return update
        assert observation.error is not None
        if observation.error.kind in {
            ToolFailureKind.TIMEOUT,
            ToolFailureKind.DEPENDENCY,
            ToolFailureKind.RESOURCE,
        }:
            return update
        failed = proposal.model_copy(
            update={"status": RefundProposalStatus.FAILED}
        )
        return {
            **update,
            "refund_proposal": failed.model_dump(mode="json"),
            "refund_proposal_history": _replace_refund_proposal(
                state.get("refund_proposal_history", []),
                failed,
            ),
            **self._terminal_update(
                AgentRunStatus.FAILED,
                AgentTerminationReason.TOOL_RUNTIME_ERROR,
                error_code=observation.error.code,
                current_node="execute_refund",
            ),
        }

    def _verify_refund_node(
        self,
        state: AgentGraphState,
    ) -> dict[str, Any]:
        proposal_document = state.get("refund_proposal")
        approval_document = state.get("refund_approval")
        if proposal_document is None or approval_document is None:
            return self._terminal_update(
                AgentRunStatus.FAILED,
                AgentTerminationReason.REFUND_VERIFICATION_FAILED,
                error_code="refund_verification_binding_missing",
                current_node="verify_refund",
            )
        proposal = RefundProposal.model_validate(proposal_document)
        approval = RefundApprovalRecord.model_validate(approval_document)
        reviewer = _actor_from_approval(approval)
        try:
            observation = self._tool_executor.execute(
                ToolCallRequest(
                    tool_name="refund_status_lookup",
                    tool_version="1.0.0",
                    arguments={
                        "idempotency_key": proposal.idempotency_key,
                    },
                    context=ToolExecutionContext(
                        actor=reviewer,
                        request_id=state["request_id"],
                        trace_id=state["trace_id"],
                        agent_run_id=state["run_id"],
                        agent_step_id="refund-verify",
                    ),
                )
            )
        except ToolCallRecordingError:
            return self._terminal_update(
                AgentRunStatus.FAILED,
                AgentTerminationReason.REFUND_VERIFICATION_FAILED,
                error_code="refund_status_call_recording_failed",
                current_node="verify_refund",
            )
        verified = (
            observation.succeeded
            and isinstance(observation.output, RefundObservation)
            and observation.output.status
            is RefundExecutionStatus.PROCESSED
            and observation.output.order_id == proposal.order_id
            and observation.output.amount_minor == proposal.amount_minor
            and observation.output.currency == proposal.currency
            and observation.output.proposal_hash
            == proposal.proposal_hash
            and observation.output.approval_id
            == approval.approval_id
            and observation.output.idempotency_key
            == proposal.idempotency_key
        )
        status = (
            RefundProposalStatus.EXECUTED
            if verified
            else RefundProposalStatus.FAILED
        )
        checked = proposal.model_copy(update={"status": status})
        update: dict[str, Any] = {
            "observations": [
                *state["observations"],
                _encode_observation(observation),
            ],
            "refund_proposal": checked.model_dump(mode="json"),
            "refund_proposal_history": _replace_refund_proposal(
                state.get("refund_proposal_history", []),
                checked,
            ),
            "refund_audit_events": [
                *state.get("refund_audit_events", []),
                _refund_audit_event(
                    event_type="refund_execution_verified",
                    occurred_at=self._now(),
                    actor_id=reviewer.actor_id,
                    details={
                        "verified": verified,
                        "proposal_version": proposal.version,
                        "tool_call_id": str(observation.call_id),
                    },
                ),
            ],
            "current_node": "verify_refund",
            "updated_at": self._now().isoformat(),
        }
        if verified:
            return {
                **update,
                "final_outcome": (
                    InvestigationOutcome.REFUND_COMPLETED.value
                ),
                "final_summary": (
                    "The approved refund was executed idempotently and "
                    "verified against the payment-system status."
                ),
                **self._terminal_update(
                    AgentRunStatus.COMPLETED,
                    AgentTerminationReason.COMPLETED,
                    current_node="verify_refund",
                ),
            }
        return {
            **update,
            **self._terminal_update(
                AgentRunStatus.ESCALATED,
                AgentTerminationReason.REFUND_VERIFICATION_FAILED,
                error_code="refund_post_execution_verification_failed",
                current_node="verify_refund",
            ),
        }

    def _verify_node(
        self,
        state: AgentGraphState,
    ) -> dict[str, Any]:
        pending = _require_pending(state)
        decision = AgentDecision.model_validate(pending["decision"])
        if decision.action is not AgentActionKind.FINISH:
            return self._terminal_update(
                AgentRunStatus.FAILED,
                AgentTerminationReason.PLANNER_ERROR,
                error_code="checkpoint_action_mismatch",
                current_node="verify",
            )
        verification = self._verifier.verify(
            self._runtime_state(state),
            decision,
        )
        trace = _pending_trace(
            state["step_count"],
            pending,
            verification_code=verification.code,
        )
        update: dict[str, Any] = {
            "steps": [*state["steps"], trace],
            "pending_planner": None,
            "current_node": "verify",
            "updated_at": self._now().isoformat(),
        }
        if verification.accepted:
            return {
                **update,
                "final_outcome": (
                    decision.outcome.value
                    if decision.outcome is not None
                    else None
                ),
                "final_summary": decision.final_summary,
                **self._terminal_update(
                    AgentRunStatus.COMPLETED,
                    AgentTerminationReason.COMPLETED,
                    current_node="verify",
                ),
            }
        update["verification_failures"] = [
            *state["verification_failures"],
            verification.code,
        ]
        return update

    @staticmethod
    def _route_after_control(state: AgentGraphState) -> str:
        return (
            "plan"
            if state["status"] == AgentRunStatus.RUNNING.value
            else "end"
        )

    @staticmethod
    def _route_after_plan(state: AgentGraphState) -> str:
        if state["status"] != AgentRunStatus.RUNNING.value:
            return "end"
        if state["pending_planner"] is None:
            return "control"
        pending = _require_pending(state)
        action = pending["decision"]["action"]
        if action == AgentActionKind.CALL_TOOL.value:
            return "execute_tool"
        if action == AgentActionKind.FINISH.value:
            return "verify"
        if action == AgentActionKind.PROPOSE_REFUND.value:
            return "evaluate_refund"
        return "end"

    @staticmethod
    def _route_after_verify(state: AgentGraphState) -> str:
        return (
            "control"
            if state["status"] == AgentRunStatus.RUNNING.value
            else "end"
        )

    @staticmethod
    def _route_after_refund_evaluation(
        state: AgentGraphState,
    ) -> str:
        return (
            "refund_approval"
            if (
                state["status"] == AgentRunStatus.RUNNING.value
                and state.get("refund_proposal") is not None
            )
            else "end"
        )

    @staticmethod
    def _route_after_refund_approval(
        state: AgentGraphState,
    ) -> str:
        if state["status"] != AgentRunStatus.RUNNING.value:
            return "end"
        proposal_document = state.get("refund_proposal")
        if proposal_document is None:
            return "end"
        proposal = RefundProposal.model_validate(proposal_document)
        if proposal.status is RefundProposalStatus.PENDING_APPROVAL:
            return "refund_approval"
        if proposal.status is RefundProposalStatus.APPROVED:
            return "execute_refund"
        return "end"

    @staticmethod
    def _route_after_refund_execution(
        state: AgentGraphState,
    ) -> str:
        return (
            "verify_refund"
            if state["status"] == AgentRunStatus.RUNNING.value
            else "end"
        )

    def _pre_step_limit(
        self,
        state: AgentGraphState,
    ) -> AgentTerminationReason | None:
        if state["step_count"] >= state["max_steps"]:
            return AgentTerminationReason.MAX_STEPS_EXCEEDED
        now = self._now()
        elapsed = (
            now - _parse_datetime(state["started_at"])
        ).total_seconds() - state["paused_duration_seconds"]
        if state["paused_at"] is not None:
            elapsed -= max(
                0.0,
                (
                    now - _parse_datetime(state["paused_at"])
                ).total_seconds(),
            )
        if elapsed >= state["max_duration_seconds"]:
            return AgentTerminationReason.TIME_BUDGET_EXCEEDED
        return None

    def _runtime_state(
        self,
        state: AgentGraphState,
    ) -> AgentRunState:
        return AgentRunState(
            run_id=UUID(state["run_id"]),
            tenant_id=UUID(state["tenant_id"]),
            actor_id=state["actor_id"],
            ticket_id=UUID(state["ticket_id"]),
            order_id=state["order_id"],
            category=TicketCategory(state["category"]),
            goal=state["goal"],
            request_id=state["request_id"],
            trace_id=state["trace_id"],
            max_steps=state["max_steps"],
            max_duration_seconds=state["max_duration_seconds"],
            max_total_tokens=state["max_total_tokens"],
            max_same_decision_attempts=(
                state["max_same_decision_attempts"]
            ),
            status=AgentRunStatus(state["status"]),
            termination_reason=(
                AgentTerminationReason(state["termination_reason"])
                if state["termination_reason"] is not None
                else None
            ),
            terminal_error_code=state["terminal_error_code"],
            step_count=state["step_count"],
            model_input_tokens=state["model_input_tokens"],
            model_output_tokens=state["model_output_tokens"],
            observations=tuple(
                self._decode_observation(item)
                for item in state["observations"]
            ),
            steps=tuple(
                _decode_step(item) for item in state["steps"]
            ),
            verification_failures=tuple(
                state["verification_failures"]
            ),
            final_outcome=(
                InvestigationOutcome(state["final_outcome"])
                if state["final_outcome"] is not None
                else None
            ),
            final_summary=state["final_summary"],
            started_at=_parse_datetime(state["started_at"]),
            updated_at=_parse_datetime(state["updated_at"]),
            completed_at=(
                _parse_datetime(state["completed_at"])
                if state["completed_at"] is not None
                else None
            ),
        )

    def _decode_observation(
        self,
        document: dict[str, Any],
    ) -> ToolObservation:
        output = None
        if document["output"] is not None:
            try:
                definition = self._registry.resolve(
                    document["tool_name"],
                    document["tool_version"],
                )
            except ToolNotFoundError as exc:
                raise AgentCheckpointCompatibilityError(
                    "A checkpoint references an unavailable tool version."
                ) from exc
            output = definition.output_model.model_validate(
                document["output"]
            )
        return ToolObservation(
            call_id=UUID(document["call_id"]),
            tenant_id=UUID(document["tenant_id"]),
            actor_id=document["actor_id"],
            tool_name=document["tool_name"],
            tool_version=document["tool_version"],
            input_schema_hash=document["input_schema_hash"],
            output_schema_hash=document["output_schema_hash"],
            status=ToolCallStatus(document["status"]),
            risk_level=(
                ToolRiskLevel(document["risk_level"])
                if document["risk_level"] is not None
                else None
            ),
            side_effect=(
                ToolSideEffect(document["side_effect"])
                if document["side_effect"] is not None
                else None
            ),
            output=output,
            error=(
                ToolErrorDetail.model_validate(document["error"])
                if document["error"] is not None
                else None
            ),
            arguments_hash=document["arguments_hash"],
            duration_ms=document["duration_ms"],
            request_id=document["request_id"],
            trace_id=document["trace_id"],
            agent_run_id=document["agent_run_id"],
            agent_step_id=document["agent_step_id"],
        )

    def _initial_state(
        self,
        command: AgentRunCommand,
        run_id: UUID,
        *,
        pause_after_step: int | None,
    ) -> AgentGraphState:
        now = self._now().isoformat()
        return AgentGraphState(
            schema_version=self.STATE_SCHEMA_VERSION,
            run_id=str(run_id),
            tenant_id=str(command.actor.tenant_id),
            actor_id=command.actor.actor_id,
            actor_roles=sorted(
                role.value for role in command.actor.roles
            ),
            ticket_id=str(command.ticket_id),
            order_id=command.order_id,
            category=command.category.value,
            goal=command.goal,
            request_id=command.request_id,
            trace_id=command.trace_id,
            max_steps=self._budget.max_steps,
            max_duration_seconds=self._budget.max_duration_seconds,
            max_total_tokens=self._budget.max_total_tokens,
            max_same_decision_attempts=(
                self._budget.max_same_decision_attempts
            ),
            status=AgentRunStatus.RUNNING.value,
            termination_reason=None,
            terminal_error_code=None,
            step_count=0,
            model_input_tokens=0,
            model_output_tokens=0,
            observations=[],
            steps=[],
            verification_failures=[],
            final_outcome=None,
            final_summary=None,
            started_at=now,
            updated_at=now,
            completed_at=None,
            current_node="start",
            decision_counts={},
            pending_planner=None,
            pause_requested=False,
            cancel_requested=False,
            pause_after_step=pause_after_step,
            paused_at=None,
            paused_duration_seconds=0.0,
            refund_proposal=None,
            refund_proposal_history=[],
            refund_approval=None,
            refund_audit_events=[],
            refund_last_modifier_actor_id=None,
        )

    def _view(self, snapshot) -> DurableAgentRunView:
        values = dict(snapshot.values)
        schema_version = values.get("schema_version")
        if schema_version != self.STATE_SCHEMA_VERSION:
            raise AgentCheckpointCompatibilityError(
                f"Unsupported Agent checkpoint schema {schema_version!r}."
            )
        configurable = snapshot.config.get("configurable", {})
        return DurableAgentRunView(
            state=self._runtime_state(values),
            paused=_snapshot_is_paused(snapshot),
            next_nodes=tuple(snapshot.next),
            interrupt_payloads=tuple(
                item.value
                for task in snapshot.tasks
                for item in task.interrupts
            ),
            checkpoint_id=configurable.get("checkpoint_id"),
            refund_proposal=(
                RefundProposal.model_validate(
                    values["refund_proposal"]
                )
                if values.get("refund_proposal") is not None
                else None
            ),
            refund_proposal_history=tuple(
                RefundProposal.model_validate(item)
                for item in values.get(
                    "refund_proposal_history",
                    [],
                )
            ),
            refund_approval=(
                RefundApprovalRecord.model_validate(
                    values["refund_approval"]
                )
                if values.get("refund_approval") is not None
                else None
            ),
            refund_audit_events=tuple(
                values.get("refund_audit_events", [])
            ),
        )

    def _require_snapshot(self, run_id: UUID):
        snapshot = self._graph.get_state(self._config(run_id))
        if not snapshot.values:
            raise AgentRunNotFoundError(str(run_id))
        return snapshot

    def _terminal_update(
        self,
        status: AgentRunStatus,
        reason: AgentTerminationReason,
        *,
        error_code: str | None = None,
        current_node: str,
    ) -> dict[str, Any]:
        now = self._now().isoformat()
        return {
            "status": status.value,
            "termination_reason": reason.value,
            "terminal_error_code": error_code,
            "current_node": current_node,
            "updated_at": now,
            "completed_at": now,
            "pause_requested": False,
        }

    def _now(self) -> datetime:
        value = self._wall_clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _config(run_id: UUID) -> dict[str, dict[str, str]]:
        return {"configurable": {"thread_id": str(run_id)}}


def _require_pending(
    state: AgentGraphState,
) -> dict[str, Any]:
    pending = state["pending_planner"]
    if pending is None:
        raise AgentCheckpointCompatibilityError(
            "The checkpoint has no pending planner decision."
        )
    return pending


def _actor_from_state(
    state: AgentGraphState,
) -> AuthenticatedActor:
    return AuthenticatedActor(
        actor_id=state["actor_id"],
        tenant_id=UUID(state["tenant_id"]),
        roles=frozenset(Role(item) for item in state["actor_roles"]),
    )


def _encode_observation(
    observation: ToolObservation,
) -> dict[str, Any]:
    return {
        "call_id": str(observation.call_id),
        "tenant_id": str(observation.tenant_id),
        "actor_id": observation.actor_id,
        "tool_name": observation.tool_name,
        "tool_version": observation.tool_version,
        "input_schema_hash": observation.input_schema_hash,
        "output_schema_hash": observation.output_schema_hash,
        "status": observation.status.value,
        "risk_level": (
            observation.risk_level.value
            if observation.risk_level is not None
            else None
        ),
        "side_effect": (
            observation.side_effect.value
            if observation.side_effect is not None
            else None
        ),
        "output": (
            observation.output.model_dump(mode="json")
            if observation.output is not None
            else None
        ),
        "error": (
            observation.error.model_dump(mode="json")
            if observation.error is not None
            else None
        ),
        "arguments_hash": observation.arguments_hash,
        "duration_ms": observation.duration_ms,
        "request_id": observation.request_id,
        "trace_id": observation.trace_id,
        "agent_run_id": observation.agent_run_id,
        "agent_step_id": observation.agent_step_id,
    }


def _pending_trace(
    step_number: int,
    pending: dict[str, Any],
    *,
    tool_call_id: str | None = None,
    tool_status: str | None = None,
    tool_error_kind: str | None = None,
    verification_code: str | None = None,
) -> dict[str, Any]:
    return {
        "step_number": step_number,
        "decision": pending["decision"],
        "model_call_id": pending["model_call_id"],
        "planner_input_tokens": pending["input_tokens"],
        "planner_output_tokens": pending["output_tokens"],
        "tool_call_id": tool_call_id,
        "tool_status": tool_status,
        "tool_error_kind": tool_error_kind,
        "verification_code": verification_code,
    }


def _decode_step(document: dict[str, Any]) -> AgentStepTrace:
    return AgentStepTrace(
        step_number=document["step_number"],
        decision=AgentDecision.model_validate(document["decision"]),
        model_call_id=(
            UUID(document["model_call_id"])
            if document["model_call_id"] is not None
            else None
        ),
        planner_input_tokens=document["planner_input_tokens"],
        planner_output_tokens=document["planner_output_tokens"],
        tool_call_id=(
            UUID(document["tool_call_id"])
            if document["tool_call_id"] is not None
            else None
        ),
        tool_status=document["tool_status"],
        tool_error_kind=document["tool_error_kind"],
        verification_code=document["verification_code"],
    )


def _decision_signature(decision: AgentDecision) -> str:
    document = decision.model_dump(mode="json", exclude_none=True)
    serialized = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(serialized).hexdigest()


def _tool_terminal_reason(
    observation: ToolObservation,
) -> tuple[
    AgentRunStatus,
    AgentTerminationReason,
    str,
] | None:
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


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _snapshot_is_paused(snapshot) -> bool:
    return any(task.interrupts for task in snapshot.tasks)


def _last_tool_name(state: AgentGraphState) -> str | None:
    if not state["observations"]:
        return None
    return state["observations"][-1]["tool_name"]


def _refund_interrupt_payload(snapshot) -> dict[str, Any] | None:
    for task in snapshot.tasks:
        for item in task.interrupts:
            if (
                isinstance(item.value, dict)
                and item.value.get("kind") == "refund_approval"
            ):
                return item.value
    return None


def _refund_audit_event(
    *,
    event_type: str,
    occurred_at: datetime,
    actor_id: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "event_type": event_type,
        "occurred_at": occurred_at.isoformat(),
        "actor_id": actor_id,
        "details": details,
    }


def _replace_refund_proposal(
    history: list[dict[str, Any]],
    proposal: RefundProposal,
) -> list[dict[str, Any]]:
    replacement = proposal.model_dump(mode="json")
    replaced = False
    result: list[dict[str, Any]] = []
    for item in history:
        if (
            item["proposal_id"] == str(proposal.proposal_id)
            and item["version"] == proposal.version
        ):
            result.append(replacement)
            replaced = True
        else:
            result.append(item)
    if not replaced:
        result.append(replacement)
    return result


def _validate_refund_review_response(
    state: AgentGraphState,
    proposal: RefundProposal,
    response: Any,
) -> dict[str, Any]:
    invalid = {
        "actor": None,
        "actor_id": None,
        "error_code": "refund_review_payload_invalid",
    }
    if not isinstance(response, dict):
        return invalid
    try:
        action = RefundReviewAction(response.get("action"))
        reviewer_document = response["reviewer"]
        reviewer = AuthenticatedActor(
            actor_id=reviewer_document["actor_id"],
            tenant_id=UUID(reviewer_document["tenant_id"]),
            roles=frozenset(
                Role(item) for item in reviewer_document["roles"]
            ),
        )
        expected_version = int(
            response["expected_proposal_version"]
        )
        expected_hash = str(response["expected_proposal_hash"])
        reason = str(response["reason"])
    except (KeyError, TypeError, ValueError):
        return invalid
    result = {
        "actor": reviewer,
        "actor_id": reviewer.actor_id,
        "error_code": None,
    }
    if reviewer.tenant_id != proposal.tenant_id:
        result["error_code"] = "refund_review_cross_tenant"
    elif not reviewer.has_permission(Permission.REFUND_APPROVE):
        result["error_code"] = "refund_review_permission_denied"
    elif reviewer.actor_id == proposal.requester_actor_id:
        result["error_code"] = "refund_review_four_eyes_required"
    elif (
        action is RefundReviewAction.APPROVE
        and reviewer.actor_id
        == state.get("refund_last_modifier_actor_id")
    ):
        result["error_code"] = (
            "refund_review_modifier_cannot_approve"
        )
    elif (
        expected_version != proposal.version
        or expected_hash != proposal.proposal_hash
    ):
        result["error_code"] = "refund_review_binding_mismatch"
    elif len(reason.strip()) < 3 or len(reason) > 500:
        result["error_code"] = "refund_review_reason_invalid"
    elif (
        action is RefundReviewAction.MODIFY
        and response.get("modified_proposal") is None
    ):
        result["error_code"] = "refund_review_modification_missing"
    elif (
        action is not RefundReviewAction.MODIFY
        and response.get("modified_proposal") is not None
    ):
        result["error_code"] = "refund_review_unexpected_modification"
    return result


def _actor_from_approval(
    approval: RefundApprovalRecord,
) -> AuthenticatedActor:
    return AuthenticatedActor(
        actor_id=approval.reviewer_actor_id,
        tenant_id=approval.reviewer_tenant_id,
        roles=frozenset(Role(item) for item in approval.reviewer_roles),
    )
