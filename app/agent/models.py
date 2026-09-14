from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from re import fullmatch
from typing import Any
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from app.domain.auth import AuthenticatedActor
from app.domain.ticket import TicketCategory
from app.tools.contracts import ToolObservation


class AgentActionKind(StrEnum):
    CALL_TOOL = "call_tool"
    FINISH = "finish"
    ESCALATE = "escalate"
    PROPOSE_REFUND = "propose_refund"


class AgentRunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    ESCALATED = "escalated"
    CANCELLED = "cancelled"
    FAILED = "failed"


class AgentTerminationReason(StrEnum):
    COMPLETED = "completed"
    HUMAN_ESCALATION = "human_escalation"
    CANCELLED = "cancelled"
    POLICY_DENIED = "policy_denied"
    APPROVAL_REJECTED = "approval_rejected"
    APPROVAL_EXPIRED = "approval_expired"
    HUMAN_TAKEOVER = "human_takeover"
    REFUND_VERIFICATION_FAILED = "refund_verification_failed"
    MAX_STEPS_EXCEEDED = "max_steps_exceeded"
    TIME_BUDGET_EXCEEDED = "time_budget_exceeded"
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"
    LOOP_DETECTED = "loop_detected"
    PLANNER_ERROR = "planner_error"
    TOOL_RUNTIME_ERROR = "tool_runtime_error"
    TOOL_PERMISSION_DENIED = "tool_permission_denied"
    TOOL_CONTRACT_ERROR = "tool_contract_error"


class InvestigationOutcome(StrEnum):
    NO_REFUNDABLE_PAYMENT = "no_refundable_payment"
    DELIVERY_IN_PROGRESS = "delivery_in_progress"
    HUMAN_REVIEW_REQUIRED = "human_review_required"
    REFUND_COMPLETED = "refund_completed"


class RefundProposalCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    amount_minor: int = Field(gt=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    reason: str = Field(min_length=5, max_length=300)


class AgentToolArguments(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    order_id: str | None = Field(
        default=None,
        min_length=3,
        max_length=64,
    )
    category: TicketCategory | None = None
    region: str | None = Field(
        default=None,
        pattern=r"^[A-Z]{2}$",
    )

    def as_tool_arguments(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class AgentDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: AgentActionKind
    reason: str = Field(min_length=3, max_length=500)
    tool_name: str | None = Field(default=None, max_length=64)
    tool_version: str | None = Field(default=None, max_length=32)
    arguments: AgentToolArguments | None = None
    outcome: InvestigationOutcome | None = None
    final_summary: str | None = Field(
        default=None,
        min_length=3,
        max_length=500,
    )
    escalation_reason: str | None = Field(
        default=None,
        min_length=3,
        max_length=500,
    )
    refund_proposal: RefundProposalCandidate | None = None

    @model_validator(mode="after")
    def validate_action_fields(self) -> "AgentDecision":
        if self.action is AgentActionKind.CALL_TOOL:
            if not self.tool_name or not self.tool_version:
                raise ValueError("Tool calls require name and version.")
            if self.arguments is None:
                raise ValueError("Tool calls require arguments.")
            if any(
                value is not None
                for value in (
                    self.outcome,
                    self.final_summary,
                    self.escalation_reason,
                    self.refund_proposal,
                )
            ):
                raise ValueError("Tool calls cannot contain terminal fields.")
        elif self.action is AgentActionKind.FINISH:
            if self.outcome is None or self.final_summary is None:
                raise ValueError("Finish requires outcome and summary.")
            if any(
                value is not None
                for value in (
                    self.tool_name,
                    self.tool_version,
                    self.arguments,
                    self.escalation_reason,
                    self.refund_proposal,
                )
            ):
                raise ValueError("Finish contains incompatible fields.")
        elif self.action is AgentActionKind.ESCALATE:
            if self.escalation_reason is None:
                raise ValueError("Escalation requires a reason.")
            if any(
                value is not None
                for value in (
                    self.tool_name,
                    self.tool_version,
                    self.arguments,
                    self.outcome,
                    self.final_summary,
                    self.refund_proposal,
                )
            ):
                raise ValueError("Escalation contains incompatible fields.")
        elif self.action is AgentActionKind.PROPOSE_REFUND:
            if self.refund_proposal is None:
                raise ValueError(
                    "Refund proposal action requires a candidate."
                )
            if any(
                value is not None
                for value in (
                    self.tool_name,
                    self.tool_version,
                    self.arguments,
                    self.outcome,
                    self.final_summary,
                    self.escalation_reason,
                )
            ):
                raise ValueError(
                    "Refund proposal contains incompatible fields."
                )
        return self


@dataclass(frozen=True, slots=True)
class AgentBudget:
    max_steps: int = 8
    max_duration_seconds: float = 30
    max_total_tokens: int = 5000
    max_same_decision_attempts: int = 2

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ValueError("max_steps must be positive.")
        if self.max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be positive.")
        if self.max_total_tokens < 1:
            raise ValueError("max_total_tokens must be positive.")
        if self.max_same_decision_attempts < 1:
            raise ValueError(
                "max_same_decision_attempts must be positive."
            )


@dataclass(frozen=True, slots=True)
class AgentRunCommand:
    actor: AuthenticatedActor
    ticket_id: UUID
    order_id: str
    category: TicketCategory
    goal: str
    request_id: str
    trace_id: str

    def __post_init__(self) -> None:
        if not fullmatch(r"[A-Za-z0-9_-]{3,64}", self.order_id):
            raise ValueError("order_id has an invalid format.")
        if not self.goal.strip():
            raise ValueError("goal must not be empty.")
        if not self.request_id or not self.trace_id:
            raise ValueError("request_id and trace_id are required.")


@dataclass(frozen=True, slots=True)
class PlannerResult:
    decision: AgentDecision
    input_tokens: int = 0
    output_tokens: int = 0
    model_call_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.input_tokens < 0 or self.output_tokens < 0:
            raise ValueError("Planner token usage cannot be negative.")


@dataclass(frozen=True, slots=True)
class CompletionVerification:
    accepted: bool
    code: str
    missing_evidence: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentStepTrace:
    step_number: int
    decision: AgentDecision
    model_call_id: UUID | None
    planner_input_tokens: int
    planner_output_tokens: int
    tool_call_id: UUID | None = None
    tool_status: str | None = None
    tool_error_kind: str | None = None
    verification_code: str | None = None


@dataclass(frozen=True, slots=True)
class AgentRunState:
    run_id: UUID
    tenant_id: UUID
    actor_id: str
    ticket_id: UUID
    order_id: str
    category: TicketCategory
    goal: str
    request_id: str
    trace_id: str
    max_steps: int
    max_duration_seconds: float
    max_total_tokens: int
    max_same_decision_attempts: int
    status: AgentRunStatus
    termination_reason: AgentTerminationReason | None
    terminal_error_code: str | None
    step_count: int
    model_input_tokens: int
    model_output_tokens: int
    observations: tuple[ToolObservation, ...]
    steps: tuple[AgentStepTrace, ...]
    verification_failures: tuple[str, ...]
    final_outcome: InvestigationOutcome | None
    final_summary: str | None
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None
