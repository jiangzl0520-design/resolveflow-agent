from enum import StrEnum

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)


class EvidenceKind(StrEnum):
    ORDER_STATUS = "order_status"
    PAYMENT_STATUS = "payment_status"
    LOGISTICS_TRACE = "logistics_trace"
    DELIVERY_PROOF = "delivery_proof"
    APPLICABLE_POLICY = "applicable_policy"
    CUSTOMER_CLARIFICATION = "customer_clarification"


class TriageRiskFlag(StrEnum):
    REFUND_REQUESTED = "refund_requested"
    DELIVERY_DISPUTE = "delivery_dispute"
    CONFLICTING_CLAIM = "conflicting_claim"
    INSUFFICIENT_INFORMATION = "insufficient_information"


class InvestigationTriage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    normalized_goal: str = Field(min_length=5, max_length=300)
    required_evidence: list[EvidenceKind] = Field(
        min_length=1,
        max_length=6,
    )
    risk_flags: list[TriageRiskFlag] = Field(
        default_factory=list,
        max_length=4,
    )
    needs_human_attention: bool
    decision_summary: str = Field(min_length=5, max_length=500)

    @field_validator("required_evidence", "risk_flags")
    @classmethod
    def values_must_be_unique(cls, values: list) -> list:
        if len(values) != len(set(values)):
            raise ValueError("List values must be unique.")
        return values
