from dataclasses import dataclass, field
from enum import StrEnum
from os import environ

DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://resolveflow:resolveflow@localhost:5432/resolveflow"
)
DEFAULT_REDIS_URL = "redis://localhost:6379/0"
DEFAULT_DEVELOPMENT_JWT_SECRET = (
    "resolveflow-development-only-jwt-secret-change-before-production"
)
DEFAULT_DEVELOPMENT_MCP_JWT_SECRET = (
    "resolveflow-development-only-mcp-secret-change-before-production"
)


class AppEnvironment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class TraceExporter(StrEnum):
    NONE = "none"
    CONSOLE = "console"
    OTLP = "otlp"


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    redis_url: str = DEFAULT_REDIS_URL
    environment: AppEnvironment = AppEnvironment.DEVELOPMENT
    jwt_secret: str = DEFAULT_DEVELOPMENT_JWT_SECRET
    jwt_issuer: str = "resolveflow-api"
    jwt_audience: str = "resolveflow-clients"
    access_token_expire_minutes: int = 30
    job_max_attempts: int = 3
    job_retry_base_seconds: int = 2
    job_retry_max_seconds: int = 60
    job_lease_seconds: int = 90
    task_soft_time_limit_seconds: int = 45
    task_time_limit_seconds: int = 60
    broker_visibility_timeout_seconds: int = 120
    llm_provider: str = "openai"
    llm_model: str = "gpt-5.6-terra"
    llm_reasoning_effort: str = "low"
    llm_timeout_seconds: float = 30.0
    llm_max_attempts: int = 3
    llm_retry_base_seconds: float = 1.0
    llm_max_output_tokens: int = 800
    llm_context_window_tokens: int = 1_050_000
    llm_max_input_tokens: int = 12_000
    llm_reserved_reasoning_tokens: int = 4_000
    llm_context_safety_margin_tokens: int = 1_000
    openai_api_key: str | None = field(default=None, repr=False)
    knowledge_embedding_model: str = "text-embedding-3-small"
    knowledge_embedding_timeout_seconds: float = 15.0
    knowledge_embedding_max_attempts: int = 3
    knowledge_embedding_retry_base_seconds: float = 0.5
    mcp_jwt_secret: str = field(
        default=DEFAULT_DEVELOPMENT_MCP_JWT_SECRET,
        repr=False,
    )
    mcp_jwt_issuer: str = "https://auth.resolveflow.local/agent-runtime"
    mcp_order_server_url: str = "http://127.0.0.1:8011/mcp"
    mcp_token_expire_seconds: int = 60
    mcp_timeout_seconds: float = 2.0
    mcp_circuit_failure_threshold: int = 3
    mcp_circuit_recovery_seconds: float = 15.0
    trace_exporter: TraceExporter = TraceExporter.NONE
    trace_service_name: str = "resolveflow-api"
    trace_otlp_endpoint: str = "http://127.0.0.1:4318/v1/traces"
    trace_sample_ratio: float = 1.0
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        if not isinstance(self.environment, AppEnvironment):
            object.__setattr__(
                self,
                "environment",
                AppEnvironment(self.environment),
            )
        if not isinstance(self.trace_exporter, TraceExporter):
            object.__setattr__(
                self,
                "trace_exporter",
                TraceExporter(self.trace_exporter),
            )
        if len(self.jwt_secret) < 32:
            raise ValueError("JWT_SECRET must contain at least 32 characters.")
        if not self.jwt_issuer or not self.jwt_audience:
            raise ValueError("JWT issuer and audience must not be empty.")
        if self.access_token_expire_minutes < 1:
            raise ValueError("ACCESS_TOKEN_EXPIRE_MINUTES must be positive.")
        if self.job_max_attempts < 1:
            raise ValueError("JOB_MAX_ATTEMPTS must be positive.")
        if not 1 <= self.job_retry_base_seconds <= self.job_retry_max_seconds:
            raise ValueError("Job retry delay settings are inconsistent.")
        if not (
            self.task_soft_time_limit_seconds
            < self.task_time_limit_seconds
            < self.job_lease_seconds
            < self.broker_visibility_timeout_seconds
        ):
            raise ValueError(
                "Expected soft limit < hard limit < lease < visibility timeout."
            )
        if self.llm_provider != "openai":
            raise ValueError("LLM_PROVIDER must currently be 'openai'.")
        if not self.llm_model.strip():
            raise ValueError("LLM_MODEL must not be empty.")
        if self.llm_reasoning_effort not in {
            "none",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        }:
            raise ValueError("Unsupported LLM_REASONING_EFFORT.")
        if self.llm_timeout_seconds <= 0:
            raise ValueError("LLM_TIMEOUT_SECONDS must be positive.")
        if self.llm_max_attempts < 1:
            raise ValueError("LLM_MAX_ATTEMPTS must be positive.")
        if self.llm_retry_base_seconds <= 0:
            raise ValueError("LLM_RETRY_BASE_SECONDS must be positive.")
        if self.llm_max_output_tokens < 1:
            raise ValueError("LLM_MAX_OUTPUT_TOKENS must be positive.")
        if self.llm_context_window_tokens < 1:
            raise ValueError("LLM_CONTEXT_WINDOW_TOKENS must be positive.")
        if self.llm_max_input_tokens < 1:
            raise ValueError("LLM_MAX_INPUT_TOKENS must be positive.")
        if self.llm_reserved_reasoning_tokens < 0:
            raise ValueError(
                "LLM_RESERVED_REASONING_TOKENS must not be negative."
            )
        if self.llm_context_safety_margin_tokens < 0:
            raise ValueError(
                "LLM_CONTEXT_SAFETY_MARGIN_TOKENS must not be negative."
            )
        reserved = (
            self.llm_max_output_tokens
            + self.llm_reserved_reasoning_tokens
            + self.llm_context_safety_margin_tokens
        )
        if reserved >= self.llm_context_window_tokens:
            raise ValueError(
                "LLM context reservations leave no room for model input."
            )
        if not self.knowledge_embedding_model.strip():
            raise ValueError("KNOWLEDGE_EMBEDDING_MODEL must not be empty.")
        if self.knowledge_embedding_timeout_seconds <= 0:
            raise ValueError(
                "KNOWLEDGE_EMBEDDING_TIMEOUT_SECONDS must be positive."
            )
        if self.knowledge_embedding_max_attempts < 1:
            raise ValueError(
                "KNOWLEDGE_EMBEDDING_MAX_ATTEMPTS must be positive."
            )
        if self.knowledge_embedding_retry_base_seconds <= 0:
            raise ValueError(
                "KNOWLEDGE_EMBEDDING_RETRY_BASE_SECONDS must be positive."
            )
        if len(self.mcp_jwt_secret) < 32:
            raise ValueError(
                "MCP_JWT_SECRET must contain at least 32 characters."
            )
        if not self.mcp_jwt_issuer.startswith(("http://", "https://")):
            raise ValueError("MCP_JWT_ISSUER must be an absolute URL.")
        if not self.mcp_order_server_url.startswith(("http://", "https://")):
            raise ValueError("MCP_ORDER_SERVER_URL must be an absolute URL.")
        if self.mcp_token_expire_seconds < 1:
            raise ValueError("MCP_TOKEN_EXPIRE_SECONDS must be positive.")
        if self.mcp_timeout_seconds <= 0:
            raise ValueError("MCP_TIMEOUT_SECONDS must be positive.")
        if self.mcp_circuit_failure_threshold < 1:
            raise ValueError(
                "MCP_CIRCUIT_FAILURE_THRESHOLD must be positive."
            )
        if self.mcp_circuit_recovery_seconds <= 0:
            raise ValueError(
                "MCP_CIRCUIT_RECOVERY_SECONDS must be positive."
            )
        if not self.trace_service_name.strip():
            raise ValueError("OTEL_SERVICE_NAME must not be empty.")
        if not self.trace_otlp_endpoint.startswith(("http://", "https://")):
            raise ValueError(
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT must be an absolute URL."
            )
        if not 0.0 <= self.trace_sample_ratio <= 1.0:
            raise ValueError("OTEL_TRACES_SAMPLER_ARG must be between 0 and 1.")
        if self.log_level.upper() not in {
            "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"
        }:
            raise ValueError("LOG_LEVEL is invalid.")
        if (
            self.environment is AppEnvironment.PRODUCTION
            and self.jwt_secret == DEFAULT_DEVELOPMENT_JWT_SECRET
        ):
            raise ValueError(
                "Production must not use the development JWT secret."
            )
        if (
            self.environment is AppEnvironment.PRODUCTION
            and self.mcp_jwt_secret
            == DEFAULT_DEVELOPMENT_MCP_JWT_SECRET
        ):
            raise ValueError(
                "Production must not use the development MCP JWT secret."
            )

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
            redis_url=environ.get("REDIS_URL", DEFAULT_REDIS_URL),
            environment=AppEnvironment(
                environ.get("APP_ENV", AppEnvironment.DEVELOPMENT.value)
            ),
            jwt_secret=environ.get(
                "JWT_SECRET",
                DEFAULT_DEVELOPMENT_JWT_SECRET,
            ),
            jwt_issuer=environ.get("JWT_ISSUER", "resolveflow-api"),
            jwt_audience=environ.get(
                "JWT_AUDIENCE",
                "resolveflow-clients",
            ),
            access_token_expire_minutes=int(
                environ.get("ACCESS_TOKEN_EXPIRE_MINUTES", "30")
            ),
            job_max_attempts=int(
                environ.get("JOB_MAX_ATTEMPTS", "3")
            ),
            job_retry_base_seconds=int(
                environ.get("JOB_RETRY_BASE_SECONDS", "2")
            ),
            job_retry_max_seconds=int(
                environ.get("JOB_RETRY_MAX_SECONDS", "60")
            ),
            job_lease_seconds=int(
                environ.get("JOB_LEASE_SECONDS", "90")
            ),
            task_soft_time_limit_seconds=int(
                environ.get("TASK_SOFT_TIME_LIMIT_SECONDS", "45")
            ),
            task_time_limit_seconds=int(
                environ.get("TASK_TIME_LIMIT_SECONDS", "60")
            ),
            broker_visibility_timeout_seconds=int(
                environ.get(
                    "BROKER_VISIBILITY_TIMEOUT_SECONDS",
                    "120",
                )
            ),
            llm_provider=environ.get("LLM_PROVIDER", "openai"),
            llm_model=environ.get("LLM_MODEL", "gpt-5.6-terra"),
            llm_reasoning_effort=environ.get(
                "LLM_REASONING_EFFORT",
                "low",
            ),
            llm_timeout_seconds=float(
                environ.get("LLM_TIMEOUT_SECONDS", "30")
            ),
            llm_max_attempts=int(
                environ.get("LLM_MAX_ATTEMPTS", "3")
            ),
            llm_retry_base_seconds=float(
                environ.get("LLM_RETRY_BASE_SECONDS", "1")
            ),
            llm_max_output_tokens=int(
                environ.get("LLM_MAX_OUTPUT_TOKENS", "800")
            ),
            llm_context_window_tokens=int(
                environ.get("LLM_CONTEXT_WINDOW_TOKENS", "1050000")
            ),
            llm_max_input_tokens=int(
                environ.get("LLM_MAX_INPUT_TOKENS", "12000")
            ),
            llm_reserved_reasoning_tokens=int(
                environ.get("LLM_RESERVED_REASONING_TOKENS", "4000")
            ),
            llm_context_safety_margin_tokens=int(
                environ.get("LLM_CONTEXT_SAFETY_MARGIN_TOKENS", "1000")
            ),
            openai_api_key=environ.get("OPENAI_API_KEY"),
            knowledge_embedding_model=environ.get(
                "KNOWLEDGE_EMBEDDING_MODEL",
                "text-embedding-3-small",
            ),
            knowledge_embedding_timeout_seconds=float(
                environ.get(
                    "KNOWLEDGE_EMBEDDING_TIMEOUT_SECONDS",
                    "15",
                )
            ),
            knowledge_embedding_max_attempts=int(
                environ.get("KNOWLEDGE_EMBEDDING_MAX_ATTEMPTS", "3")
            ),
            knowledge_embedding_retry_base_seconds=float(
                environ.get(
                    "KNOWLEDGE_EMBEDDING_RETRY_BASE_SECONDS",
                    "0.5",
                )
            ),
            mcp_jwt_secret=environ.get(
                "MCP_JWT_SECRET",
                DEFAULT_DEVELOPMENT_MCP_JWT_SECRET,
            ),
            mcp_jwt_issuer=environ.get(
                "MCP_JWT_ISSUER",
                "https://auth.resolveflow.local/agent-runtime",
            ),
            mcp_order_server_url=environ.get(
                "MCP_ORDER_SERVER_URL",
                "http://127.0.0.1:8011/mcp",
            ),
            mcp_token_expire_seconds=int(
                environ.get("MCP_TOKEN_EXPIRE_SECONDS", "60")
            ),
            mcp_timeout_seconds=float(
                environ.get("MCP_TIMEOUT_SECONDS", "2")
            ),
            mcp_circuit_failure_threshold=int(
                environ.get("MCP_CIRCUIT_FAILURE_THRESHOLD", "3")
            ),
            mcp_circuit_recovery_seconds=float(
                environ.get("MCP_CIRCUIT_RECOVERY_SECONDS", "15")
            ),
            trace_exporter=TraceExporter(
                environ.get("OTEL_TRACES_EXPORTER", TraceExporter.NONE.value)
            ),
            trace_service_name=environ.get(
                "OTEL_SERVICE_NAME",
                "resolveflow-api",
            ),
            trace_otlp_endpoint=environ.get(
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
                "http://127.0.0.1:4318/v1/traces",
            ),
            trace_sample_ratio=float(
                environ.get("OTEL_TRACES_SAMPLER_ARG", "1.0")
            ),
            log_level=environ.get("LOG_LEVEL", "INFO"),
        )
