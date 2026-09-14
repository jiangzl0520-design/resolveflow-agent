from collections.abc import Iterable

from app.agent.models import (
    AgentDecision,
    AgentRunState,
    PlannerResult,
)
from app.tools.contracts import ToolDescriptor


class ScriptedAgentPlanner:
    def __init__(
        self,
        outcomes: Iterable[
            AgentDecision | PlannerResult | Exception
        ],
    ) -> None:
        self._outcomes = list(outcomes)
        self.states: list[AgentRunState] = []
        self.tool_catalogs: list[tuple[ToolDescriptor, ...]] = []

    def decide(
        self,
        state: AgentRunState,
        allowed_tools: tuple[ToolDescriptor, ...],
    ) -> PlannerResult:
        self.states.append(state)
        self.tool_catalogs.append(allowed_tools)
        if not self._outcomes:
            raise AssertionError("Scripted planner has no outcome.")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, PlannerResult):
            return outcome
        return PlannerResult(decision=outcome)
