from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
import json
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.agent.models import AgentRunState, RefundProposalCandidate
from app.domain.investigation_triage import EvidenceKind
from app.tools.after_sales import (
    LogisticsObservation,
    OrderObservation,
    OrderStatus,
    PolicyObservation,
)
from app.observability.tracing import (
    FailureDomain,
    add_safe_event,
    operation_span,
    set_safe_attributes,
)
from app.observability.metrics import get_metrics


class RefundProposalStatus(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    EXECUTED = "executed"
    FAILED = "failed"


class RefundProposal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_id: UUID
    version: int = Field(ge=1)
    proposal_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(min_length=16, max_length=96)
    status: RefundProposalStatus
    tenant_id: UUID
    run_id: UUID
    ticket_id: UUID
    order_id: str
    requester_actor_id: str
    amount_minor: int = Field(gt=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    reason: str = Field(min_length=5, max_length=300)
    policy_id: str
    policy_version: str
    evidence_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: datetime
    expires_at: datetime


class RefundPolicyEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool
    code: str
    proposal: RefundProposal | None = None
    missing_evidence: tuple[str, ...] = ()


class RefundPolicyEngine:
    """Deterministic safety boundary for model-proposed refunds."""

    def __init__(
        self,
        *,
        approval_ttl: timedelta = timedelta(hours=24),
        evidence_max_age: timedelta = timedelta(days=7),
    ) -> None:
        if approval_ttl.total_seconds() <= 0:
            raise ValueError("approval_ttl must be positive.")
        if evidence_max_age.total_seconds() <= 0:
            raise ValueError("evidence_max_age must be positive.")
        self._approval_ttl = approval_ttl
        self._evidence_max_age = evidence_max_age

    def evaluate(
        self,
        state: AgentRunState,
        candidate: RefundProposalCandidate,
        *,
        now: datetime,
        version: int = 1,
    ) -> RefundPolicyEvaluation:
        with operation_span(
            "evaluate refund policy",
            failure_domain=FailureDomain.POLICY,
            attributes={
                "resolveflow.component": "policy",
                "resolveflow.policy.name": "refund_eligibility",
                "resolveflow.policy.proposal_version": version,
                "resolveflow.agent.run_id": state.run_id,
                "resolveflow.request_id": state.request_id,
                "resolveflow.trace_id": state.trace_id,
            },
        ) as span:
            result = self._evaluate(
                state,
                candidate,
                now=now,
                version=version,
            )
            get_metrics().record_policy(
                policy="refund_eligibility",
                allowed=result.allowed,
                code=result.code,
            )
            set_safe_attributes(
                span,
                {
                    "resolveflow.policy.allowed": result.allowed,
                    "resolveflow.policy.code": result.code,
                    "resolveflow.policy.missing_evidence": (
                        result.missing_evidence
                    ),
                    "resolveflow.policy.proposal_id": (
                        result.proposal.proposal_id
                        if result.proposal is not None
                        else None
                    ),
                    "resolveflow.policy.version": (
                        result.proposal.policy_version
                        if result.proposal is not None
                        else None
                    ),
                },
            )
            if not result.allowed:
                set_safe_attributes(
                    span,
                    {
                        "resolveflow.failure.domain": (
                            FailureDomain.POLICY.value
                        ),
                        "resolveflow.error.code": result.code,
                    },
                )
                add_safe_event(
                    span,
                    "policy.denied",
                    {"code": result.code},
                )
            return result

    def _evaluate(
        self,
        state: AgentRunState,
        candidate: RefundProposalCandidate,
        *,
        now: datetime,
        version: int = 1,
    ) -> RefundPolicyEvaluation:
        current_time = _as_utc(now)
        order = _latest_output(
            state,
            OrderObservation,
            lambda item: item.order_id == state.order_id,
        )
        logistics = _latest_output(
            state,
            LogisticsObservation,
            lambda item: item.order_id == state.order_id,
        )
        policy = _latest_output(
            state,
            PolicyObservation,
            lambda item: item.category is state.category,
        )

        missing: list[str] = []
        if order is None:
            missing.append(EvidenceKind.ORDER_STATUS.value)
        if logistics is None:
            missing.append(EvidenceKind.LOGISTICS_TRACE.value)
        if policy is None:
            missing.append(EvidenceKind.APPLICABLE_POLICY.value)
        if missing:
            return RefundPolicyEvaluation(
                allowed=False,
                code="refund_required_evidence_missing",
                missing_evidence=tuple(missing),
            )

        assert order is not None
        assert logistics is not None
        assert policy is not None
        if order.status in {
            OrderStatus.PENDING_PAYMENT,
            OrderStatus.CANCELLED,
            OrderStatus.REFUNDED,
        } or not order.paid:
            return RefundPolicyEvaluation(
                allowed=False,
                code="order_not_refundable",
            )
        if candidate.amount_minor > order.amount_minor:
            return RefundPolicyEvaluation(
                allowed=False,
                code="refund_amount_exceeds_order_total",
            )
        if candidate.currency != order.currency:
            return RefundPolicyEvaluation(
                allowed=False,
                code="refund_currency_mismatch",
            )
        if policy.effective_at > current_time:
            return RefundPolicyEvaluation(
                allowed=False,
                code="refund_policy_not_effective",
            )
        if not policy.requires_human_approval:
            return RefundPolicyEvaluation(
                allowed=False,
                code="refund_policy_missing_human_gate",
            )
        if _is_stale(order.observed_at, current_time, self._evidence_max_age):
            return RefundPolicyEvaluation(
                allowed=False,
                code="order_evidence_stale",
            )
        if _is_stale(
            logistics.observed_at,
            current_time,
            self._evidence_max_age,
        ):
            return RefundPolicyEvaluation(
                allowed=False,
                code="logistics_evidence_stale",
            )

        missing_required = _missing_required_evidence(
            policy,
            order=order,
            logistics=logistics,
        )
        if missing_required:
            return RefundPolicyEvaluation(
                allowed=False,
                code="refund_policy_evidence_unsatisfied",
                missing_evidence=missing_required,
            )

        proposal_id = uuid4()
        immutable_payload = {
            "proposal_id": str(proposal_id),
            "version": version,
            "tenant_id": str(state.tenant_id),
            "run_id": str(state.run_id),
            "ticket_id": str(state.ticket_id),
            "order_id": state.order_id,
            "requester_actor_id": state.actor_id,
            "amount_minor": candidate.amount_minor,
            "currency": candidate.currency,
            "reason": candidate.reason,
            "policy_id": policy.policy_id,
            "policy_version": policy.version,
            "evidence_hash": _evidence_hash(order, logistics, policy),
        }
        proposal_hash = _document_hash(immutable_payload)
        idempotency_key = (
            f"refund-{_document_hash({
                'tenant_id': str(state.tenant_id),
                'run_id': str(state.run_id),
                'proposal_hash': proposal_hash,
            })}"
        )
        proposal = RefundProposal(
            **immutable_payload,
            proposal_hash=proposal_hash,
            idempotency_key=idempotency_key,
            status=RefundProposalStatus.PENDING_APPROVAL,
            created_at=current_time,
            expires_at=current_time + self._approval_ttl,
        )
        return RefundPolicyEvaluation(
            allowed=True,
            code="refund_candidate_policy_eligible",
            proposal=proposal,
        )


def _latest_output(state, output_type, predicate):
    for observation in reversed(state.observations):
        if (
            observation.succeeded
            and isinstance(observation.output, output_type)
            and predicate(observation.output)
        ):
            return observation.output
    return None


def _missing_required_evidence(
    policy: PolicyObservation,
    *,
    order: OrderObservation,
    logistics: LogisticsObservation,
) -> tuple[str, ...]:
    available = {
        EvidenceKind.ORDER_STATUS,
        EvidenceKind.LOGISTICS_TRACE,
        EvidenceKind.APPLICABLE_POLICY,
    }
    if order.paid:
        available.add(EvidenceKind.PAYMENT_STATUS)
    if logistics.proof_available:
        available.add(EvidenceKind.DELIVERY_PROOF)
    return tuple(
        item.value
        for item in policy.required_evidence
        if item not in available
    )


def _evidence_hash(
    order: OrderObservation,
    logistics: LogisticsObservation,
    policy: PolicyObservation,
) -> str:
    return _document_hash(
        {
            "order": order.model_dump(mode="json"),
            "logistics": logistics.model_dump(mode="json"),
            "policy": policy.model_dump(mode="json"),
        }
    )


def _document_hash(document: dict) -> str:
    serialized = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(serialized).hexdigest()


def _is_stale(
    observed_at: datetime,
    now: datetime,
    max_age: timedelta,
) -> bool:
    normalized = _as_utc(observed_at)
    return normalized > now or now - normalized > max_age


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
