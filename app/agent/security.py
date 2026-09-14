from dataclasses import dataclass
import re

from app.agent.models import AgentActionKind, AgentDecision, AgentRunState
from app.context_engineering.agent_context import relevant_tool_names
from app.security.content import UntrustedContentGuard
from app.tools.contracts import ToolDescriptor


@dataclass(frozen=True, slots=True)
class AgentDecisionSecurityResult:
    allowed: bool
    code: str


class AgentDecisionSecurityPolicy:
    def __init__(
        self,
        *,
        guard: UntrustedContentGuard | None = None,
        protected_values: tuple[str, ...] = (),
    ) -> None:
        self._guard = guard or UntrustedContentGuard()
        self._protected_fragments = _protected_fragments(protected_values)

    def verify(
        self,
        state: AgentRunState,
        decision: AgentDecision,
        available_tools: tuple[ToolDescriptor, ...],
    ) -> AgentDecisionSecurityResult:
        output_text = "\n".join(
            value
            for value in (
                decision.reason,
                decision.final_summary,
                decision.escalation_reason,
                (
                    decision.refund_proposal.reason
                    if decision.refund_proposal is not None
                    else None
                ),
            )
            if value is not None
        )
        inspected = self._guard.inspect_text(output_text)
        if inspected.sensitive_data_detected or _contains_protected_fragment(
            output_text,
            self._protected_fragments,
        ):
            return AgentDecisionSecurityResult(
                False,
                "agent_output_security_blocked",
            )

        if decision.action is not AgentActionKind.CALL_TOOL:
            return AgentDecisionSecurityResult(True, "agent_decision_allowed")

        assert decision.tool_name is not None
        assert decision.tool_version is not None
        assert decision.arguments is not None
        available = {
            (item.name, item.version) for item in available_tools
        }
        if (
            (decision.tool_name, decision.tool_version) in available
            and decision.tool_name not in relevant_tool_names(state)
        ):
            return AgentDecisionSecurityResult(
                False,
                "agent_tool_not_allowed_for_step",
            )

        arguments = decision.arguments
        if decision.tool_name in {"order_lookup", "logistics_lookup"}:
            if (
                arguments.order_id is not None
                and arguments.order_id != state.order_id
            ):
                return AgentDecisionSecurityResult(
                    False,
                    "agent_tool_resource_binding_mismatch",
                )
        elif decision.tool_name == "policy_lookup":
            if (
                arguments.category is not None
                and arguments.category is not state.category
            ):
                return AgentDecisionSecurityResult(
                    False,
                    "agent_tool_resource_binding_mismatch",
                )
        return AgentDecisionSecurityResult(True, "agent_decision_allowed")


def _protected_fragments(values: tuple[str, ...]) -> tuple[str, ...]:
    fragments: set[str] = set()
    for value in values:
        for item in re.split(r"(?<=[.!?。！？])\s+", value):
            normalized = (
                " ".join(item.split()).casefold().strip(" .!?。！？")
            )
            if len(normalized) >= 40:
                fragments.add(normalized)
    return tuple(sorted(fragments))


def _contains_protected_fragment(
    value: str,
    protected_fragments: tuple[str, ...],
) -> bool:
    normalized = " ".join(value.split()).casefold().strip(" .!?。！？")
    return any(fragment in normalized for fragment in protected_fragments)
