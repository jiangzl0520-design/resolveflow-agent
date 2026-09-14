from argparse import ArgumentParser
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import platform
from uuid import UUID, NAMESPACE_URL, uuid5

from app.domain.auth import AuthenticatedActor, Role
from app.domain.grounded_answer import GroundedAnswerQuery
from app.domain.retrieval import (
    KnowledgeRetrievalRun,
    KnowledgeSearchHit,
    KnowledgeSearchResult,
    RetrievalMode,
    RewrittenKnowledgeQuery,
)
from app.knowledge.answering_errors import GroundingVerificationError
from app.llm.contracts import RawProviderResponse
from app.llm.fake_provider import FakeLLMProvider
from app.llm.gateway import ModelGateway
from app.repositories.knowledge_answer_repository import (
    InMemoryKnowledgeAnswerRepository,
)
from app.repositories.model_call_repository import (
    InMemoryModelCallRepository,
)
from app.services.grounded_answer_service import GroundedAnswerService

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = (
    ROOT / "evaluation" / "datasets" / "day14_grounded_answer_v1.json"
)
TENANT_ID = UUID("e4000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 8, 4, 17, 0, tzinfo=UTC)
ACTOR = AuthenticatedActor(
    actor_id="day14-evaluation-agent",
    tenant_id=TENANT_ID,
    roles=frozenset({Role.AGENT}),
)


class StaticSearcher:
    def __init__(self, hits: tuple[KnowledgeSearchHit, ...]) -> None:
        self._hits = hits

    def search(self, actor, query):
        return KnowledgeSearchResult(
            run=KnowledgeRetrievalRun(
                id=uuid5(NAMESPACE_URL, f"day14-run:{query.trace_id}"),
                tenant_id=actor.tenant_id,
                actor_id=actor.actor_id,
                actor_roles=tuple(actor.roles),
                query_hash="a" * 64,
                rewritten_query_hash="b" * 64,
                rewrite_strategy="evaluation-fixture",
                search_mode=query.mode,
                embedding_model="day14-synthetic-retrieval",
                top_k=query.top_k,
                semantic_candidate_count=len(self._hits),
                keyword_candidate_count=len(self._hits),
                result_count=len(self._hits),
                degraded_reason=None,
                latency_ms=0.0,
                request_id=query.request_id,
                trace_id=query.trace_id,
                created_at=NOW,
            ),
            rewritten_query=RewrittenKnowledgeQuery(
                semantic_text=query.text,
                keyword_terms=(query.text,),
                strategy="evaluation-fixture",
            ),
            hits=self._hits,
        )


def _hit(
    identity: str,
    content: str,
    *,
    source_key: str = "policy/delivery-dispute",
    version: str = "1.0.0",
    effective_to: datetime | None = NOW + timedelta(days=1),
) -> KnowledgeSearchHit:
    return KnowledgeSearchHit(
        chunk_id=uuid5(NAMESPACE_URL, f"day14-chunk:{identity}"),
        document_id=uuid5(NAMESPACE_URL, f"day14-document:{identity}"),
        source_key=source_key,
        title="签收争议政策",
        content=content,
        source_uri=f"repo://policies/{identity}.md",
        document_version=version,
        source_line_start=1,
        source_line_end=1,
        effective_from=NOW - timedelta(days=1),
        effective_to=effective_to,
        fused_score=1.0,
        semantic_rank=1,
        keyword_rank=1,
        vector_distance=0.0,
        keyword_score=1.0,
    )


def _raw(output: object) -> RawProviderResponse:
    return RawProviderResponse(
        output=output,
        model="day14-deterministic-model-fixture",
        input_tokens=10,
        output_tokens=5,
    )


def _assessment(count: int) -> dict[str, object]:
    return {
        "assessments": [
            {
                "evidence_id": f"K{index}",
                "relevance": 95,
                "relation": "direct",
            }
            for index in range(1, count + 1)
        ],
        "conflicts": [],
    }


def _draft(
    claim: str,
    quote: str,
    *,
    evidence_id: str = "K1",
) -> dict[str, object]:
    return {
        "disposition": "answered",
        "claims": [
            {
                "text": claim,
                "supports": [
                    {
                        "evidence_id": evidence_id,
                        "exact_quote": quote,
                    }
                ],
            }
        ],
    }


def _cases(dataset: dict[str, object]) -> list[dict[str, object]]:
    counts = dataset["scenario_counts"]
    assert isinstance(counts, dict)
    cases: list[dict[str, object]] = []
    for scenario, count in counts.items():
        for index in range(int(count)):
            cases.append(
                {
                    "id": f"{scenario}-{index:02d}",
                    "scenario": scenario,
                }
            )
    return cases


def _fixtures(
    case: dict[str, object],
    dataset: dict[str, object],
) -> tuple[tuple[KnowledgeSearchHit, ...], list[RawProviderResponse]]:
    identity = str(case["id"])
    scenario = str(case["scenario"])
    valid_policy = str(dataset["valid_policy"])
    claim = str(dataset["expected_claim"])
    quote = str(dataset["valid_quote"])
    valid = _hit(identity, valid_policy)
    if scenario == "answerable":
        return (valid,), [_raw(_assessment(1)), _raw(_draft(claim, quote))]
    if scenario == "insufficient":
        return (), []
    if scenario == "conflict":
        conflicting = _hit(
            f"{identity}-new",
            str(dataset["conflicting_policy"]),
            version="2.0.0",
        )
        return (valid, conflicting), [_raw(_assessment(2))]
    if scenario == "expired":
        return (
            _hit(identity, valid_policy, effective_to=NOW),
        ), []
    if scenario == "forged_citation":
        return (valid,), [
            _raw(_assessment(1)),
            _raw(_draft(claim, "不存在的原文", evidence_id="K9")),
        ]
    raise AssertionError(f"Unknown scenario: {scenario}")


def _verified_result(
    case: dict[str, object],
    dataset: dict[str, object],
) -> dict[str, object]:
    hits, outcomes = _fixtures(case, dataset)
    provider = FakeLLMProvider(outcomes)
    call_repository = InMemoryModelCallRepository()
    answer_repository = InMemoryKnowledgeAnswerRepository()
    service = GroundedAnswerService(
        StaticSearcher(hits),
        ModelGateway(
            provider,
            call_repository,
            model="day14-deterministic-model-fixture",
            reasoning_effort="low",
            max_output_tokens=800,
            max_attempts=1,
            retry_base_seconds=1,
            wall_clock=lambda: NOW,
            monotonic_clock=lambda: 1.0,
        ),
        answer_repository,
        wall_clock=lambda: NOW,
        monotonic_clock=lambda: 1.0,
    )
    identity = str(case["id"])
    try:
        result = service.answer(
            ACTOR,
            GroundedAnswerQuery(
                question=str(dataset["question"]),
                as_of=NOW,
                retrieval_mode=RetrievalMode.HYBRID,
                candidate_k=10,
                max_evidence=5,
                request_id=f"day14-eval-request-{identity}",
                trace_id=f"day14-eval-trace-{identity}",
            ),
        )
        return {
            "status": result.status.value,
            "citation_count": len(result.citations),
            "valid_citation_count": sum(
                citation.exact_quote in str(dataset["valid_policy"])
                and str(case["scenario"]) == "answerable"
                for citation in result.citations
            ),
            "model_call_count": len(call_repository.records),
            "run_recorded": len(answer_repository.runs) == 1,
        }
    except GroundingVerificationError:
        return {
            "status": "blocked_forged_citation",
            "citation_count": 0,
            "valid_citation_count": 0,
            "model_call_count": len(call_repository.records),
            "run_recorded": (
                len(answer_repository.runs) == 1
                and answer_repository.runs[0].status.value == "failed"
            ),
        }


def _baseline_result(case: dict[str, object]) -> dict[str, object]:
    # This baseline represents accepting one structured claim/citation without
    # effective-time, conflict, existence, or exact-quote verification.
    return {
        "status": "answered",
        "citation_count": 1,
        "valid_citation_count": int(case["scenario"] == "answerable"),
    }


def _is_correct(scenario: str, status: str) -> bool:
    expected = {
        "answerable": "answered",
        "insufficient": "insufficient_evidence",
        "conflict": "evidence_conflict",
        "expired": "insufficient_evidence",
        "forged_citation": "blocked_forged_citation",
    }
    return status == expected[scenario]


def _percent(numerator: int, denominator: int) -> float:
    return round(numerator / denominator * 100, 2) if denominator else 0.0


def run(dataset_path: Path) -> dict[str, object]:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    cases = _cases(dataset)
    results: list[dict[str, object]] = []
    for case in cases:
        baseline = _baseline_result(case)
        verified = _verified_result(case, dataset)
        scenario = str(case["scenario"])
        results.append(
            {
                **case,
                "baseline": baseline,
                "verified": verified,
                "baseline_correct": _is_correct(
                    scenario,
                    str(baseline["status"]),
                ),
                "verified_correct": _is_correct(
                    scenario,
                    str(verified["status"]),
                ),
            }
        )

    baseline_citations = sum(
        int(item["baseline"]["citation_count"]) for item in results
    )
    baseline_valid = sum(
        int(item["baseline"]["valid_citation_count"]) for item in results
    )
    verified_citations = sum(
        int(item["verified"]["citation_count"])
        for item in results
        if item["verified"]["status"] == "answered"
    )
    verified_valid = sum(
        int(item["verified"]["valid_citation_count"]) for item in results
    )
    baseline_correct = sum(bool(item["baseline_correct"]) for item in results)
    verified_correct = sum(bool(item["verified_correct"]) for item in results)
    forged = [item for item in results if item["scenario"] == "forged_citation"]
    return {
        "report_id": "day14-grounded-answer-v1",
        "dataset": {
            "dataset_id": dataset["dataset_id"],
            "synthetic": dataset["synthetic"],
            "case_count": len(results),
            "scenario_counts": dataset["scenario_counts"],
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "model_provider": "deterministic structured-output fixture",
            "repository": "deterministic in-memory evaluation adapter",
            "paid_api_calls": 0,
        },
        "baseline": {
            "name": "structured-output-only-without-grounding-gates",
            "correct_handling_percent": _percent(
                baseline_correct,
                len(results),
            ),
            "citation_precision_percent": _percent(
                baseline_valid,
                baseline_citations,
            ),
            "expired_policy_citations": sum(
                int(item["baseline"]["citation_count"])
                for item in results
                if item["scenario"] == "expired"
            ),
            "forged_citations_blocked": 0,
        },
        "verified_pipeline": {
            "correct_handling_percent": _percent(
                verified_correct,
                len(results),
            ),
            "citation_precision_percent": _percent(
                verified_valid,
                verified_citations,
            ),
            "expired_policy_citations": sum(
                int(item["verified"]["citation_count"])
                for item in results
                if item["scenario"] == "expired"
            ),
            "forged_citations_blocked": sum(
                item["verified"]["status"] == "blocked_forged_citation"
                for item in forged
            ),
            "runs_recorded_percent": _percent(
                sum(bool(item["verified"]["run_recorded"]) for item in results),
                len(results),
            ),
            "model_calls_recorded": sum(
                int(item["verified"]["model_call_count"])
                for item in results
            ),
        },
        "measured_value": {
            "correct_handling_lift_points": round(
                _percent(verified_correct, len(results))
                - _percent(baseline_correct, len(results)),
                2,
            ),
            "citation_precision_lift_points": round(
                _percent(verified_valid, verified_citations)
                - _percent(baseline_valid, baseline_citations),
                2,
            ),
        },
        "case_results": results,
        "interpretation_limits": [
            "All policies, questions, model outputs, and expected outcomes are synthetic and deterministic.",
            "The report measures orchestration and deterministic safety gates, not production language-model quality.",
            "Real PostgreSQL, pgvector retrieval, foreign keys, model-call traces, and citation persistence are verified separately by integration tests.",
            "Production value still requires a labeled dataset sampled from real support traffic.",
        ],
    }


def main() -> None:
    parser = ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
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
