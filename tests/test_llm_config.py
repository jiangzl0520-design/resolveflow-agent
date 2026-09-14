import pytest

from app.core.config import AppEnvironment, Settings
from app.llm.errors import ModelConfigurationError
from app.llm.runtime import create_openai_model_gateway_runtime

TEST_SECRET = "test-only-secret-with-more-than-thirty-two-characters"


def settings(**overrides) -> Settings:
    values = {
        "database_url": "sqlite+pysqlite:///:memory:",
        "environment": AppEnvironment.TEST,
        "jwt_secret": TEST_SECRET,
    }
    values.update(overrides)
    return Settings(**values)


def test_api_key_is_excluded_from_settings_representation() -> None:
    current = settings(openai_api_key="super-secret-api-key")

    assert "super-secret-api-key" not in repr(current)


def test_invalid_reasoning_effort_is_rejected_at_startup() -> None:
    with pytest.raises(ValueError, match="REASONING_EFFORT"):
        settings(llm_reasoning_effort="extreme")


def test_openai_runtime_requires_explicit_api_key() -> None:
    with pytest.raises(
        ModelConfigurationError,
        match="openai_api_key_missing",
    ):
        create_openai_model_gateway_runtime(
            settings(openai_api_key=None),
            session_factory=None,  # type: ignore[arg-type]
        )


def test_context_reservations_must_leave_model_input_capacity() -> None:
    with pytest.raises(ValueError, match="leave no room"):
        settings(
            llm_context_window_tokens=1_000,
            llm_max_output_tokens=500,
            llm_reserved_reasoning_tokens=400,
            llm_context_safety_margin_tokens=100,
        )
