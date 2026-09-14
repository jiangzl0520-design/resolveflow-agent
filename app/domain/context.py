from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    computed_field,
    model_validator,
)


class ContextSource(StrEnum):
    SYSTEM_POLICY = "system_policy"
    CURRENT_GOAL = "current_goal"
    COMPLETION_CRITERIA = "completion_criteria"
    AGENT_STATE = "agent_state"
    USER_CORRECTION = "user_correction"
    CONFIRMED_FACT = "confirmed_fact"
    TOOL_OBSERVATION = "tool_observation"
    RAG_EVIDENCE = "rag_evidence"
    VERIFICATION_FEEDBACK = "verification_feedback"
    HISTORY = "history"
    MEMORY = "memory"
    TOOL_DESCRIPTOR = "tool_descriptor"


class ContextTrustLevel(StrEnum):
    SYSTEM = "system"
    VERIFIED_INTERNAL = "verified_internal"
    USER_PROVIDED = "user_provided"
    EXTERNAL_DATA = "external_data"


class ContextDecisionReason(StrEnum):
    REQUIRED = "required"
    SELECTED = "selected"
    SUPERSEDED = "superseded"
    DUPLICATE = "duplicate"
    INVALIDATED = "invalidated"
    AUTHORIZATION_FILTERED = "authorization_filtered"
    LOW_RELEVANCE = "low_relevance"
    NOT_RELEVANT_TO_CURRENT_STEP = "not_relevant_to_current_step"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    SECURITY_POLICY_BLOCKED = "security_policy_blocked"
    BUILD_ABORTED = "build_aborted"
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"


class ContextBuildStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


_PREEXCLUSION_REASONS = {
    ContextDecisionReason.INVALIDATED,
    ContextDecisionReason.AUTHORIZATION_FILTERED,
    ContextDecisionReason.NOT_RELEVANT_TO_CURRENT_STEP,
    ContextDecisionReason.DEPENDENCY_UNAVAILABLE,
    ContextDecisionReason.SECURITY_POLICY_BLOCKED,
}


class ContextFragmentCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fragment_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    )
    semantic_key: str = Field(min_length=1, max_length=128)
    source: ContextSource
    trust_level: ContextTrustLevel
    content: JsonValue
    priority: int = Field(ge=0, le=100)
    relevance: int = Field(ge=0, le=100)
    ordinal: int = Field(default=0, ge=0)
    required: bool = False
    replaceable: bool = False
    eligible: bool = True
    exclusion_reason: ContextDecisionReason | None = None

    @model_validator(mode="after")
    def validate_security_contract(self) -> "ContextFragmentCandidate":
        if self.source is ContextSource.SYSTEM_POLICY and (
            self.trust_level is not ContextTrustLevel.SYSTEM
            or not self.required
        ):
            raise ValueError("System policy must be trusted and required.")
        if self.eligible and self.exclusion_reason is not None:
            raise ValueError("Eligible context cannot have an exclusion reason.")
        if not self.eligible and self.exclusion_reason not in _PREEXCLUSION_REASONS:
            raise ValueError("Ineligible context needs a preselection reason.")
        return self


class ContextWindowBudget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    context_window_tokens: int = Field(gt=0)
    reserved_output_tokens: int = Field(ge=0)
    reserved_reasoning_tokens: int = Field(ge=0)
    safety_margin_tokens: int = Field(ge=0)
    max_input_tokens: int = Field(gt=0)

    @computed_field
    @property
    def input_budget_tokens(self) -> int:
        available = self.context_window_tokens - (
            self.reserved_output_tokens
            + self.reserved_reasoning_tokens
            + self.safety_margin_tokens
        )
        return min(self.max_input_tokens, available)

    @model_validator(mode="after")
    def validate_reservations(self) -> "ContextWindowBudget":
        if self.input_budget_tokens < 1:
            raise ValueError("Context reservations leave no input budget.")
        return self


class ContextBuildRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    agent_run_id: UUID
    step_number: int = Field(ge=1)
    model: str = Field(min_length=1, max_length=100)
    budget: ContextWindowBudget
    candidates: tuple[ContextFragmentCandidate, ...] = Field(min_length=1)
    response_schema: JsonValue | None = None
    request_id: str = Field(min_length=1, max_length=64)
    trace_id: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def fragment_ids_must_be_unique(self) -> "ContextBuildRequest":
        identifiers = [item.fragment_id for item in self.candidates]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Context fragment IDs must be unique.")
        sources = {item.source for item in self.candidates}
        if ContextSource.SYSTEM_POLICY not in sources:
            raise ValueError("Context requires a system policy fragment.")
        if ContextSource.CURRENT_GOAL not in sources:
            raise ValueError("Context requires a current goal fragment.")
        return self


class SelectedContextFragment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fragment_id: str
    semantic_key: str
    source: ContextSource
    trust_level: ContextTrustLevel
    content: JsonValue
    priority: int
    relevance: int
    ordinal: int
    token_count: int = Field(ge=0)


class ContextFragmentTrace(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    build_run_id: UUID
    fragment_id: str
    semantic_key: str
    source: ContextSource
    trust_level: ContextTrustLevel
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    priority: int
    relevance: int
    ordinal: int
    estimated_tokens: int = Field(ge=0)
    included: bool
    decision_reason: ContextDecisionReason


class ContextBuildRun(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    tenant_id: UUID
    agent_run_id: UUID
    step_number: int
    model: str
    status: ContextBuildStatus
    context_window_tokens: int
    reserved_output_tokens: int
    reserved_reasoning_tokens: int
    safety_margin_tokens: int
    input_budget_tokens: int
    actual_input_tokens: int
    included_fragment_count: int
    dropped_fragment_count: int
    error_code: str | None
    request_id: str
    trace_id: str
    created_at: datetime


class ContextBuildResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run: ContextBuildRun
    instructions: str
    input_text: str
    included_fragments: tuple[SelectedContextFragment, ...]
    traces: tuple[ContextFragmentTrace, ...]
