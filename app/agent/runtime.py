from sqlalchemy.orm import Session, sessionmaker

from app.agent.planner import ModelGatewayAgentPlanner
from app.context_engineering.builder import ContextBuilder
from app.context_engineering.tokens import TiktokenTokenCounter
from app.core.config import Settings
from app.domain.context import ContextWindowBudget
from app.llm.gateway import ModelGateway
from app.repositories.sqlalchemy_context_trace_repository import (
    SqlAlchemyContextTraceRepository,
)
from app.repositories.sqlalchemy_memory_repository import (
    SqlAlchemyMemoryRepository,
)


def create_model_gateway_agent_planner(
    gateway: ModelGateway,
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> ModelGatewayAgentPlanner:
    """Wire planner and context audit to the same durable database."""
    memory_repository = SqlAlchemyMemoryRepository(session_factory)
    return ModelGatewayAgentPlanner(
        gateway,
        context_builder=ContextBuilder(
            TiktokenTokenCounter(settings.llm_model),
            SqlAlchemyContextTraceRepository(session_factory),
        ),
        context_budget=ContextWindowBudget(
            context_window_tokens=settings.llm_context_window_tokens,
            reserved_output_tokens=settings.llm_max_output_tokens,
            reserved_reasoning_tokens=settings.llm_reserved_reasoning_tokens,
            safety_margin_tokens=settings.llm_context_safety_margin_tokens,
            max_input_tokens=settings.llm_max_input_tokens,
        ),
        memory_reader=memory_repository,
    )
