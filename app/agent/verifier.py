from app.agent.models import (
    AgentDecision,
    AgentRunState,
    CompletionVerification,
    InvestigationOutcome,
)
from app.tools.after_sales import (
    LogisticsObservation,
    LogisticsStatus,
    OrderObservation,
    OrderStatus,
    PolicyObservation,
)


class InvestigationCompletionVerifier:
    def verify(
        self,
        state: AgentRunState,
        decision: AgentDecision,
    ) -> CompletionVerification:
        order = _latest_output(
            state,
            OrderObservation,
            lambda item: item.order_id == state.order_id,
        )
        if order is None:
            return CompletionVerification(
                accepted=False,
                code="missing_order_observation",
                missing_evidence=("order_status",),
            )

        if order.status in {
            OrderStatus.PENDING_PAYMENT,
            OrderStatus.CANCELLED,
            OrderStatus.REFUNDED,
        }:
            if (
                decision.outcome
                is InvestigationOutcome.NO_REFUNDABLE_PAYMENT
            ):
                return CompletionVerification(
                    accepted=True,
                    code="order_not_refundable_verified",
                )
            return CompletionVerification(
                accepted=False,
                code="outcome_conflicts_with_order",
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
        if logistics is None:
            missing.append("logistics_trace")
        if policy is None:
            missing.append("applicable_policy")
        if missing:
            return CompletionVerification(
                accepted=False,
                code="missing_required_evidence",
                missing_evidence=tuple(missing),
            )

        assert logistics is not None
        if logistics.status in {
            LogisticsStatus.CREATED,
            LogisticsStatus.IN_TRANSIT,
        }:
            expected = InvestigationOutcome.DELIVERY_IN_PROGRESS
        else:
            expected = InvestigationOutcome.HUMAN_REVIEW_REQUIRED
        if decision.outcome is not expected:
            return CompletionVerification(
                accepted=False,
                code="outcome_conflicts_with_observations",
            )
        return CompletionVerification(
            accepted=True,
            code="completion_evidence_verified",
        )


def _latest_output(
    state: AgentRunState,
    output_type,
    predicate,
):
    for observation in reversed(state.observations):
        if observation.succeeded and isinstance(
            observation.output,
            output_type,
        ) and predicate(observation.output):
            return observation.output
    return None
