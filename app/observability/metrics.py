from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from threading import Lock

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.exposition import CONTENT_TYPE_LATEST, generate_latest


LATENCY_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
SAFE_FALLBACK = "other"


def _bounded(value: object | None, allowed: Iterable[str]) -> str:
    normalized = str(value or "unknown").strip().lower()
    allowed_values = frozenset(allowed)
    return normalized if normalized in allowed_values else SAFE_FALLBACK


class ResolveFlowMetrics:
    """Low-cardinality technical and business metrics for one process."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        self.http_requests = Counter(
            "resolveflow_http_requests_total",
            "HTTP requests by stable route and status class.",
            ("method", "route", "status_class"),
            registry=self.registry,
        )
        self.http_duration = Histogram(
            "resolveflow_http_request_duration_seconds",
            "HTTP server latency.",
            ("method", "route"),
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.http_in_progress = Gauge(
            "resolveflow_http_requests_in_progress",
            "HTTP requests currently executing.",
            ("method",),
            registry=self.registry,
        )
        self.agent_runs = Counter(
            "resolveflow_agent_runs_total",
            "Agent runs by terminal result and failure domain.",
            ("workflow", "status", "termination_reason", "failure_domain"),
            registry=self.registry,
        )
        self.agent_duration = Histogram(
            "resolveflow_agent_run_duration_seconds",
            "Agent run wall-clock latency.",
            ("workflow", "status"),
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.agent_steps = Histogram(
            "resolveflow_agent_steps",
            "Number of decisions in one agent run.",
            ("workflow", "status"),
            buckets=(1, 2, 3, 4, 5, 8, 12, 20),
            registry=self.registry,
        )
        self.agent_tokens = Counter(
            "resolveflow_agent_tokens_total",
            "Agent token consumption.",
            ("workflow", "direction"),
            registry=self.registry,
        )
        self.llm_calls = Counter(
            "resolveflow_llm_calls_total",
            "LLM calls by stable provider/model/result.",
            ("provider", "model", "operation", "status", "error_code"),
            registry=self.registry,
        )
        self.llm_duration = Histogram(
            "resolveflow_llm_call_duration_seconds",
            "LLM gateway latency including retries.",
            ("provider", "model", "operation", "status"),
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.llm_tokens = Counter(
            "resolveflow_llm_tokens_total",
            "LLM input and output tokens.",
            ("provider", "model", "direction"),
            registry=self.registry,
        )
        self.tool_calls = Counter(
            "resolveflow_tool_calls_total",
            "Tool calls by contract version and typed result.",
            ("tool", "version", "status", "error_kind", "error_code"),
            registry=self.registry,
        )
        self.tool_duration = Histogram(
            "resolveflow_tool_call_duration_seconds",
            "Tool execution latency.",
            ("tool", "version", "status"),
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.retrieval_runs = Counter(
            "resolveflow_retrieval_runs_total",
            "Knowledge retrieval runs by mode and result.",
            ("mode", "status", "error_code"),
            registry=self.registry,
        )
        self.retrieval_duration = Histogram(
            "resolveflow_retrieval_duration_seconds",
            "Knowledge retrieval latency.",
            ("mode", "status"),
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.rag_answers = Counter(
            "resolveflow_business_rag_answers_total",
            "Grounded answer outcomes.",
            ("status", "failure_domain"),
            registry=self.registry,
        )
        self.database_operations = Counter(
            "resolveflow_database_operations_total",
            "Database operations by type and result.",
            ("system", "operation", "status"),
            registry=self.registry,
        )
        self.database_duration = Histogram(
            "resolveflow_database_operation_duration_seconds",
            "Database operation latency without SQL text.",
            ("system", "operation", "status"),
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.worker_runs = Counter(
            "resolveflow_worker_runs_total",
            "Worker task outcomes.",
            ("task", "outcome", "reason"),
            registry=self.registry,
        )
        self.policy_decisions = Counter(
            "resolveflow_business_policy_decisions_total",
            "Business policy outcomes, separate from system health.",
            ("policy", "decision", "code"),
            registry=self.registry,
        )
        self.human_escalations = Counter(
            "resolveflow_business_human_escalations_total",
            "Agent runs handed to a human.",
            ("workflow", "reason"),
            registry=self.registry,
        )

    def render(self) -> tuple[bytes, str]:
        return generate_latest(self.registry), CONTENT_TYPE_LATEST

    def observe_http(
        self, *, method: str, route: str, status_code: int, duration: float
    ) -> None:
        labels = (method.upper(), route, f"{status_code // 100}xx")
        self.http_requests.labels(*labels).inc()
        self.http_duration.labels(method.upper(), route).observe(duration)

    def record_agent_run(
        self,
        *,
        workflow: str,
        status: str,
        termination_reason: str | None,
        failure_domain: str,
        duration: float,
        steps: int,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        workflow = _bounded(workflow, {"agent_runner", "durable_workflow"})
        status = _bounded(status, {"completed", "failed", "escalated", "paused"})
        reason = _bounded(
            termination_reason,
            {
                "completed", "human_escalation", "max_steps_exceeded",
                "time_budget_exceeded", "token_budget_exceeded",
                "loop_detected", "planner_error", "tool_runtime_error",
                "tool_permission_denied", "tool_contract_error", "interrupted",
            },
        )
        domain = _bounded(
            failure_domain,
            {"none", "api", "worker", "agent", "model", "retrieval", "tool",
             "database", "policy", "context", "security", "business", "unknown"},
        )
        self.agent_runs.labels(workflow, status, reason, domain).inc()
        self.agent_duration.labels(workflow, status).observe(max(duration, 0.0))
        self.agent_steps.labels(workflow, status).observe(max(steps, 0))
        self.agent_tokens.labels(workflow, "input").inc(max(input_tokens, 0))
        self.agent_tokens.labels(workflow, "output").inc(max(output_tokens, 0))
        if status == "escalated":
            self.human_escalations.labels(workflow, reason).inc()

    def record_tool_call(
        self, *, tool: str, version: str, status: str,
        error_kind: str | None, error_code: str | None, duration: float,
    ) -> None:
        status = _bounded(status, {"succeeded", "failed"})
        error_kind = _bounded(
            error_kind or "none",
            {"none", "selection", "argument", "authorization", "timeout",
             "resource", "dependency", "conflict", "output_contract", "internal"},
        )
        code = _bounded(
            error_code or "none",
            {"none", "tool_not_found", "tool_permission_denied",
             "tool_arguments_invalid", "tool_timeout", "tool_resource_not_found",
             "tool_idempotency_conflict", "tool_execution_grant_invalid",
             "tool_output_invalid", "tool_execution_failed", "mcp_timeout",
             "mcp_dependency_unavailable", "mcp_authorization_failed",
             "mcp_output_invalid"},
        )
        safe_tool = _bounded(tool, {
            "order_lookup", "logistics_lookup", "policy_lookup",
            "refund_execute", "unknown_lookup", "day18_lookup",
        })
        safe_version = _bounded(version, {"1.0.0", "1.1.0", "1.2.3"})
        self.tool_calls.labels(safe_tool, safe_version, status, error_kind, code).inc()
        self.tool_duration.labels(safe_tool, safe_version, status).observe(max(duration, 0.0))

    def record_policy(self, *, policy: str, allowed: bool, code: str) -> None:
        safe_code = _bounded(code, {
            "refund_candidate_policy_eligible", "refund_required_evidence_missing",
            "order_not_refundable", "refund_amount_exceeds_order_total",
            "refund_currency_mismatch", "refund_policy_not_effective",
            "refund_policy_missing_human_gate", "order_evidence_stale",
            "logistics_evidence_stale", "refund_policy_evidence_unsatisfied",
        })
        self.policy_decisions.labels(
            _bounded(policy, {"refund_eligibility"}),
            "allowed" if allowed else "denied",
            safe_code,
        ).inc()

    def record_retrieval(
        self, *, mode: str, status: str, error_code: str | None, duration: float
    ) -> None:
        safe_mode = _bounded(mode, {"semantic", "keyword", "hybrid"})
        safe_status = _bounded(status, {"succeeded", "degraded", "failed"})
        safe_error = _bounded(error_code or "none", {
            "none", "knowledge_embedding_contract_error", "embedding_timeout",
            "knowledge_search_storage_error", "knowledge_retrieval_recording_error",
            "authorization_denied",
        })
        self.retrieval_runs.labels(safe_mode, safe_status, safe_error).inc()
        self.retrieval_duration.labels(safe_mode, safe_status).observe(max(duration, 0.0))

    def record_rag(self, *, status: str, failure_domain: str = "none") -> None:
        safe_status = _bounded(status, {
            "answered", "insufficient_evidence", "evidence_conflict",
            "security_blocked", "failed",
        })
        safe_domain = _bounded(failure_domain, {
            "none", "security", "database", "retrieval", "model", "policy", "unknown"
        })
        self.rag_answers.labels(safe_status, safe_domain).inc()

    def record_worker(self, *, task: str, outcome: str, reason: str | None) -> None:
        safe_task = _bounded(task, {"process_investigation", "recover_pending"})
        safe_outcome = _bounded(outcome, {"succeeded", "retry", "terminal", "missing"})
        safe_reason = _bounded(reason or "none", {
            "none", "cancelled", "cancellation_pending", "job_not_found", "claim_race",
            "attempts_exhausted", "ticket_not_found", "ticket_state_not_eligible",
            "investigation_started", "lease_lost", "worker_observed_cancellation",
        })
        self.worker_runs.labels(safe_task, safe_outcome, safe_reason).inc()

    def record_database(
        self, *, system: str, operation: str, status: str, duration: float
    ) -> None:
        safe_system = _bounded(system, {"postgresql", "sqlite"})
        safe_operation = _bounded(operation, {
            "select", "insert", "update", "delete", "query", "execute",
            "create", "alter", "drop", "pragma",
        })
        safe_status = _bounded(status, {"succeeded", "failed"})
        self.database_operations.labels(safe_system, safe_operation, safe_status).inc()
        self.database_duration.labels(safe_system, safe_operation, safe_status).observe(max(duration, 0.0))

    def record_llm_call(
        self, *, provider: str, model: str, operation: str, status: str,
        error_code: str | None, duration: float, input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        safe_provider = _bounded(provider, {"openai", "fake"})
        safe_model = _bounded(model, {
            "gpt-5.6-terra", "fake-model", "fake-model-v18", "configured-model"
        })
        safe_operation = _bounded(operation, {
            "chat", "triage", "agent_plan", "query_rewrite", "grounded_answer"
        })
        safe_status = _bounded(status, {"succeeded", "failed"})
        safe_error = _bounded(error_code or "none", {
            "none", "provider_timeout", "provider_unavailable",
            "provider_rejected", "provider_unclassified_error",
            "model_output_invalid", "model_call_recording_failed",
        })
        self.llm_calls.labels(
            safe_provider, safe_model, safe_operation, safe_status, safe_error
        ).inc()
        self.llm_duration.labels(
            safe_provider, safe_model, safe_operation, safe_status
        ).observe(max(duration, 0.0))
        self.llm_tokens.labels(safe_provider, safe_model, "input").inc(max(input_tokens or 0, 0))
        self.llm_tokens.labels(safe_provider, safe_model, "output").inc(max(output_tokens or 0, 0))


@dataclass(frozen=True, slots=True)
class IncidentDiagnosis:
    baseline_success_rate: float
    current_success_rate: float
    success_rate_change: float
    top_failure_domain: str
    top_failure_count: int
    attributed_failure_share: float


def diagnose_agent_success_drop(
    baseline: ResolveFlowMetrics,
    current: ResolveFlowMetrics,
) -> IncidentDiagnosis:
    base = _agent_counts(baseline.registry)
    now = _agent_counts(current.registry)
    base_total = sum(base.values())
    now_total = sum(now.values())
    base_success = sum(value for (status, _), value in base.items() if status == "completed")
    now_success = sum(value for (status, _), value in now.items() if status == "completed")
    failures = {
        domain: sum(value for (status, item_domain), value in now.items()
                    if status != "completed" and item_domain == domain)
        for domain in {domain for status, domain in now if status != "completed"}
    }
    top_domain, top_count = max(failures.items(), key=lambda item: item[1], default=("unknown", 0))
    failure_total = sum(failures.values())
    base_rate = base_success / base_total if base_total else 0.0
    current_rate = now_success / now_total if now_total else 0.0
    return IncidentDiagnosis(
        baseline_success_rate=base_rate,
        current_success_rate=current_rate,
        success_rate_change=current_rate - base_rate,
        top_failure_domain=top_domain,
        top_failure_count=int(top_count),
        attributed_failure_share=top_count / failure_total if failure_total else 0.0,
    )


def _agent_counts(registry: CollectorRegistry) -> dict[tuple[str, str], float]:
    result: dict[tuple[str, str], float] = {}
    for metric in registry.collect():
        if metric.name != "resolveflow_agent_runs":
            continue
        for sample in metric.samples:
            if sample.name.endswith("_total"):
                key = (sample.labels["status"], sample.labels["failure_domain"])
                result[key] = result.get(key, 0.0) + sample.value
    return result


_metrics_lock = Lock()
_global_metrics: ResolveFlowMetrics | None = None


def get_metrics() -> ResolveFlowMetrics:
    global _global_metrics
    if _global_metrics is None:
        with _metrics_lock:
            if _global_metrics is None:
                _global_metrics = ResolveFlowMetrics()
    return _global_metrics
