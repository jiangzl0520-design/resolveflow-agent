from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.agent.models import RefundProposalCandidate
from app.domain.auth import AuthenticatedActor


class RefundReviewAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    MODIFY = "modify"
    TAKEOVER = "takeover"


@dataclass(frozen=True, slots=True)
class RefundReviewCommand:
    actor: AuthenticatedActor
    action: RefundReviewAction
    expected_proposal_version: int
    expected_proposal_hash: str
    reason: str
    modified_proposal: RefundProposalCandidate | None = None

    def __post_init__(self) -> None:
        if self.expected_proposal_version < 1:
            raise ValueError("expected_proposal_version must be positive.")
        if len(self.expected_proposal_hash) != 64:
            raise ValueError("expected_proposal_hash must be a SHA-256 hash.")
        if len(self.reason.strip()) < 3:
            raise ValueError("Review reason must contain at least 3 characters.")
        if (
            self.action is RefundReviewAction.MODIFY
            and self.modified_proposal is None
        ):
            raise ValueError("Modify requires a replacement proposal.")
        if (
            self.action is not RefundReviewAction.MODIFY
            and self.modified_proposal is not None
        ):
            raise ValueError(
                "Only modify may contain a replacement proposal."
            )


class RefundApprovalRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    approval_id: UUID
    proposal_id: UUID
    proposal_version: int = Field(ge=1)
    proposal_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    reviewer_actor_id: str
    reviewer_tenant_id: UUID
    reviewer_roles: tuple[str, ...]
    action: RefundReviewAction
    reason: str = Field(min_length=3, max_length=500)
    reviewed_at: datetime
