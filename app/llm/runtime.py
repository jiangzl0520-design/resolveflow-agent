from dataclasses import dataclass

from openai import OpenAI
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.llm.errors import ModelConfigurationError
from app.llm.gateway import ModelGateway
from app.llm.openai_provider import OpenAIResponsesProvider
from app.repositories.sqlalchemy_model_call_repository import (
    SqlAlchemyModelCallRepository,
)


@dataclass(slots=True)
class ModelGatewayRuntime:
    gateway: ModelGateway
    client: OpenAI

    def close(self) -> None:
        self.client.close()


def create_openai_model_gateway_runtime(
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> ModelGatewayRuntime:
    if not settings.openai_api_key:
        raise ModelConfigurationError("openai_api_key_missing")
    client = OpenAI(
        api_key=settings.openai_api_key,
        timeout=settings.llm_timeout_seconds,
        max_retries=0,
    )
    return ModelGatewayRuntime(
        gateway=ModelGateway(
            OpenAIResponsesProvider(client),
            SqlAlchemyModelCallRepository(session_factory),
            model=settings.llm_model,
            reasoning_effort=settings.llm_reasoning_effort,
            max_output_tokens=settings.llm_max_output_tokens,
            max_attempts=settings.llm_max_attempts,
            retry_base_seconds=settings.llm_retry_base_seconds,
        ),
        client=client,
    )
