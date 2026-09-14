from argparse import ArgumentParser
import json
from pathlib import Path
import platform
from uuid import UUID, uuid4

from app.domain.ticket import TicketCategory
from app.llm.contracts import RawProviderResponse
from app.llm.errors import ModelProviderTimeoutError
from app.llm.fake_provider import FakeLLMProvider
from app.llm.gateway import ModelGateway
from app.repositories.model_call_repository import (
    InMemoryModelCallRepository,
)
from app.services.investigation_triage_service import (
    InvestigationTriageService,
    TriageSource,
    TriageTicketCommand,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = (
    ROOT
    / "evaluation"
    / "datasets"
    / "day06_structured_output_v1.json"
)
TENANT_ID = UUID("60000000-0000-0000-0000-000000000001")


def _valid_output(index: int) -> dict[str, object]:
    return {
        "normalized_goal": f"调查第 {index} 个包裹未收到问题",
        "required_evidence": [
            "order_status",
            "logistics_trace",
            "delivery_proof",
            "applicable_policy",
        ],
        "risk_flags": ["delivery_dispute"],
        "needs_human_attention": False,
        "decision_summary": "先核实订单、物流、签收证明和适用政策。",
    }


def _invalid_outputs(
    config: dict[str, int],
) -> list[dict[str, object]]:
    outputs: list[dict[str, object]] = []
    index = 100
    for _ in range(config["missing_required_field"]):
        current = _valid_output(index)
        current.pop("required_evidence")
        outputs.append(current)
        index += 1
    for _ in range(config["unexpected_action_field"]):
        current = _valid_output(index)
        current["execute_refund"] = True
        outputs.append(current)
        index += 1
    for _ in range(config["invalid_evidence_enum"]):
        current = _valid_output(index)
        current["required_evidence"] = ["private_database_dump"]
        outputs.append(current)
        index += 1
    for _ in range(config["duplicate_evidence"]):
        current = _valid_output(index)
        current["required_evidence"] = [
            "order_status",
            "order_status",
        ]
        outputs.append(current)
        index += 1
    return outputs


def _command(index: int) -> TriageTicketCommand:
    return TriageTicketCommand(
        ticket_id=uuid4(),
        tenant_id=TENANT_ID,
        subject=f"合成工单 {index}",
        description="物流显示签收，但客户表示没有收到并要求退款。",
        category=TicketCategory.NOT_RECEIVED,
    )


def _run_gateway_case(
    index: int,
    outcomes,
) -> tuple[TriageSource, str | None, int]:
    provider = FakeLLMProvider(outcomes)
    service = InvestigationTriageService(
        ModelGateway(
            provider,
            InMemoryModelCallRepository(),
            model="fake-model-v1",
            reasoning_effort="low",
            max_output_tokens=500,
            max_attempts=2,
            retry_base_seconds=0.001,
            sleeper=lambda _: None,
        )
    )
    result = service.triage(
        _command(index),
        request_id=f"day06-request-{index:03d}",
        trace_id=f"day06-trace-{index:03d}",
    )
    if result.source is TriageSource.DETERMINISTIC_FALLBACK:
        assert result.decision.needs_human_attention is True
        assert len(result.decision.required_evidence) >= 3
    return result.source, result.degradation_reason, len(provider.requests)


def run(dataset_path: Path) -> dict[str, object]:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    valid_count = int(dataset["valid_immediate_cases"])
    invalid_outputs = _invalid_outputs(dataset["invalid_schema_cases"])
    timeout_count = int(dataset["transient_timeout_then_valid_cases"])

    baseline_safe_typed_decisions = valid_count
    baseline_invalid_accepted = len(invalid_outputs)
    baseline_transient_recovered = 0
    baseline_attempts = valid_count + len(invalid_outputs) + timeout_count

    model_sources = 0
    fallback_sources = 0
    invalid_accepted = 0
    transient_recovered = 0
    provider_attempts = 0
    case_index = 0

    for case_index in range(valid_count):
        source, _, attempts = _run_gateway_case(
            case_index,
            [
                RawProviderResponse(
                    output=_valid_output(case_index),
                    model="fake-model-v1",
                )
            ],
        )
        model_sources += source is TriageSource.MODEL
        fallback_sources += (
            source is TriageSource.DETERMINISTIC_FALLBACK
        )
        provider_attempts += attempts

    for invalid_output in invalid_outputs:
        case_index += 1
        source, reason, attempts = _run_gateway_case(
            case_index,
            [
                RawProviderResponse(
                    output=invalid_output,
                    model="fake-model-v1",
                )
            ],
        )
        invalid_accepted += source is TriageSource.MODEL
        fallback_sources += (
            source is TriageSource.DETERMINISTIC_FALLBACK
        )
        assert reason == "model_output_invalid"
        provider_attempts += attempts

    for _ in range(timeout_count):
        case_index += 1
        source, reason, attempts = _run_gateway_case(
            case_index,
            [
                ModelProviderTimeoutError(),
                RawProviderResponse(
                    output=_valid_output(case_index),
                    model="fake-model-v1",
                ),
            ],
        )
        recovered = (
            source is TriageSource.MODEL
            and reason is None
            and attempts == 2
        )
        transient_recovered += recovered
        model_sources += source is TriageSource.MODEL
        fallback_sources += (
            source is TriageSource.DETERMINISTIC_FALLBACK
        )
        provider_attempts += attempts

    total_cases = valid_count + len(invalid_outputs) + timeout_count
    return {
        "report_id": "day06-structured-output-v1",
        "dataset": dataset,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "provider": "FakeLLMProvider",
            "paid_api_calls": 0,
        },
        "baseline_json_only_no_gateway": {
            "total_cases": total_cases,
            "safe_typed_decisions": baseline_safe_typed_decisions,
            "invalid_outputs_entering_business_logic": (
                baseline_invalid_accepted
            ),
            "transient_failures_recovered": baseline_transient_recovered,
            "provider_attempts": baseline_attempts,
        },
        "resolveflow_model_gateway": {
            "total_cases": total_cases,
            "safe_typed_decisions": model_sources + fallback_sources,
            "model_decisions": model_sources,
            "conservative_fallback_decisions": fallback_sources,
            "invalid_outputs_entering_business_logic": invalid_accepted,
            "transient_failures_recovered": transient_recovered,
            "provider_attempts": provider_attempts,
        },
        "interpretation_limits": [
            "All examples and model outputs are synthetic.",
            "This measures schema enforcement and recovery behavior, not real-model semantic quality.",
            "Fake LLM uses zero paid tokens; production quality, latency, and cost remain unverified.",
        ],
    }


def main() -> None:
    parser = ArgumentParser()
    parser.add_argument(
        "--dataset",
        type=Path,
        default=DEFAULT_DATASET,
    )
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    report = run(arguments.dataset)
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)


if __name__ == "__main__":
    main()
