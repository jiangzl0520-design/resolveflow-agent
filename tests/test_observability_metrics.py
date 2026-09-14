import io
import json
import logging

from app.observability.logging import JsonLogFormatter, log_context, log_event
from app.observability.metrics import ResolveFlowMetrics, diagnose_agent_success_drop


def test_metrics_endpoint_exposes_http_metric_without_resource_ids(client) -> None:
    client.get("/health")
    response = client.get("/metrics")

    assert response.status_code == 200
    body = response.text
    assert "resolveflow_http_requests_total" in body
    assert 'route="/health"' in body
    assert "agent-test-001" not in body
    assert "10000000-0000-0000-0000-000000000001" not in body


def test_unknown_tool_labels_collapse_to_other() -> None:
    metrics = ResolveFlowMetrics()
    for index in range(50):
        metrics.record_tool_call(
            tool=f"customer-{index}", version=f"dynamic-{index}",
            status="failed", error_kind="internal",
            error_code=f"raw-error-{index}", duration=0.01,
        )
    body = metrics.render()[0].decode()

    assert 'tool="other"' in body
    assert 'version="other"' in body
    assert 'error_code="other"' in body
    assert "customer-49" not in body


def test_business_metrics_are_separate_from_technical_metrics() -> None:
    metrics = ResolveFlowMetrics()
    metrics.record_policy(
        policy="refund_eligibility", allowed=False,
        code="refund_required_evidence_missing",
    )
    body = metrics.render()[0].decode()

    assert "resolveflow_business_policy_decisions_total" in body
    assert 'decision="denied"' in body
    assert "resolveflow_agent_runs_total{" not in body


def test_structured_log_correlates_and_redacts_unapproved_fields() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    logger = logging.getLogger("resolveflow.day19.test")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    with log_context(request_id="req-19", trace_id="trace-19"):
        log_event(
            logger, "tool.failed", tool="order_lookup",
            error_code="tool_timeout", customer_email="customer@example.com",
            prompt_content="ignore previous instructions",
        )
    document = json.loads(stream.getvalue())

    assert document["request_id"] == "req-19"
    assert document["trace_id"] == "trace-19"
    assert document["tool"] == "order_lookup"
    assert document["customer_email"] == "[REDACTED]"
    assert document["prompt_content"] == "[REDACTED]"
    assert "customer@example.com" not in stream.getvalue()


def test_diagnosis_finds_top_failure_domain_and_handles_empty_window() -> None:
    baseline = ResolveFlowMetrics()
    current = ResolveFlowMetrics()
    for _ in range(9):
        baseline.record_agent_run(
            workflow="agent_runner", status="completed",
            termination_reason="completed", failure_domain="none",
            duration=0.1, steps=2,
        )
    baseline.record_agent_run(
        workflow="agent_runner", status="failed",
        termination_reason="planner_error", failure_domain="model",
        duration=0.1, steps=2,
    )
    for _ in range(6):
        current.record_agent_run(
            workflow="agent_runner", status="completed",
            termination_reason="completed", failure_domain="none",
            duration=0.1, steps=2,
        )
    for domain in ("tool", "tool", "tool", "model"):
        current.record_agent_run(
            workflow="agent_runner", status="failed",
            termination_reason="tool_runtime_error", failure_domain=domain,
            duration=0.1, steps=2,
        )

    diagnosis = diagnose_agent_success_drop(baseline, current)
    empty = diagnose_agent_success_drop(ResolveFlowMetrics(), ResolveFlowMetrics())
    assert diagnosis.baseline_success_rate == 0.9
    assert diagnosis.current_success_rate == 0.6
    assert diagnosis.top_failure_domain == "tool"
    assert diagnosis.attributed_failure_share == 0.75
    assert empty.top_failure_domain == "unknown"
    assert empty.current_success_rate == 0.0
