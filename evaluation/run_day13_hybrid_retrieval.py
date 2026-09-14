from argparse import ArgumentParser
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import platform
from uuid import UUID, NAMESPACE_URL, uuid5

from app.domain.auth import AuthenticatedActor, Role
from app.domain.retrieval import (
    KnowledgeRetrievalCandidate,
    KnowledgeSearchQuery,
    RetrievalMode,
)
from app.knowledge.constants import KNOWLEDGE_EMBEDDING_DIMENSIONS
from app.repositories.knowledge_search_repository import (
    InMemoryKnowledgeSearchRepository,
)
from app.services.knowledge_search_service import KnowledgeSearchService

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = (
    ROOT / "evaluation" / "datasets" / "day13_hybrid_retrieval_v1.json"
)
TENANT_ID = UUID("e3000000-0000-0000-0000-000000000001")
OTHER_TENANT_ID = UUID("e3000000-0000-0000-0000-000000000002")
NOW = datetime(2026, 8, 4, 14, 0, tzinfo=UTC)
AGENT = AuthenticatedActor(
    actor_id="day13-evaluation-agent",
    tenant_id=TENANT_ID,
    roles=frozenset({Role.AGENT}),
)


def _vector(position: int) -> tuple[float, ...]:
    values = [0.0] * KNOWLEDGE_EMBEDDING_DIMENSIONS
    values[position] = 1.0
    return tuple(values)


class SyntheticSemanticProvider:
    """Fixture embeddings for pipeline comparison, not model-quality claims."""

    model = "day13-synthetic-semantic-v1"
    dimensions = KNOWLEDGE_EMBEDDING_DIMENSIONS

    def __init__(self, policies: list[dict[str, str]]) -> None:
        self._policies = policies

    def embed(self, texts):
        return tuple(self._embed_one(text) for text in texts)

    def _embed_one(self, text: str) -> tuple[float, ...]:
        for index, policy in enumerate(self._policies):
            marker = f"[[concept:{policy['concept']}]]"
            if marker in text or policy["semantic_alias"] in text:
                return _vector(index)
        return _vector(KNOWLEDGE_EMBEDDING_DIMENSIONS - 1)


def _candidate(
    *,
    identity: str,
    source_key: str,
    title: str,
    content: str,
    tenant_id: UUID = TENANT_ID,
    roles: tuple[Role, ...] = (Role.AGENT, Role.SUPERVISOR),
    effective_from: datetime = NOW - timedelta(days=1),
    effective_to: datetime | None = NOW + timedelta(days=1),
) -> KnowledgeRetrievalCandidate:
    document_id = uuid5(NAMESPACE_URL, f"day13-document:{identity}")
    return KnowledgeRetrievalCandidate(
        chunk_id=uuid5(NAMESPACE_URL, f"day13-chunk:{identity}"),
        document_id=document_id,
        tenant_id=tenant_id,
        source_key=source_key,
        title=title,
        content=content,
        source_uri=f"repo://policies/{identity}.md",
        document_version="1.0.0",
        source_line_start=1,
        source_line_end=3,
        effective_from=effective_from,
        effective_to=effective_to,
        allowed_roles=roles,
    )


def _add(
    repository: InMemoryKnowledgeSearchRepository,
    provider: SyntheticSemanticProvider,
    candidate: KnowledgeRetrievalCandidate,
) -> None:
    repository.add_entry(
        candidate,
        content_hash=sha256(candidate.content.encode("utf-8")).hexdigest(),
        embedding=provider.embed([candidate.content])[0],
        embedding_model=provider.model,
    )


def _build_repository(
    dataset: dict[str, object],
    provider: SyntheticSemanticProvider,
) -> InMemoryKnowledgeSearchRepository:
    repository = InMemoryKnowledgeSearchRepository()
    policies = dataset["policies"]
    assert isinstance(policies, list)
    for policy in policies:
        assert isinstance(policy, dict)
        content = (
            f"[[concept:{policy['concept']}]]\n"
            f"{policy['code']}\n{policy['keyword_text']}"
        )
        _add(
            repository,
            provider,
            _candidate(
                identity=str(policy["concept"]),
                source_key=f"policy/evaluation/{policy['concept']}",
                title=str(policy["title"]),
                content=content,
            ),
        )
    _add_security_candidates(repository, provider, dataset)
    return repository


def _add_security_candidates(
    repository: InMemoryKnowledgeSearchRepository,
    provider: SyntheticSemanticProvider,
    dataset: dict[str, object],
) -> None:
    counts = dataset["security_scenario_counts"]
    policies = dataset["policies"]
    assert isinstance(counts, dict)
    assert isinstance(policies, list)
    configurations = {
        "other_tenant": {
            "tenant_id": OTHER_TENANT_ID,
        },
        "supervisor_only": {
            "roles": (Role.SUPERVISOR,),
        },
        "expired": {
            "effective_to": NOW,
        },
        "future": {
            "effective_from": NOW + timedelta(seconds=1),
            "effective_to": None,
        },
    }
    for scenario, overrides in configurations.items():
        count = int(counts[scenario])
        for index in range(count):
            policy = policies[index % len(policies)]
            assert isinstance(policy, dict)
            code = f"RF-SEC-{scenario.upper()}-{index:02d}"
            content = (
                f"[[concept:{policy['concept']}]]\n{code}\n"
                f"{policy['keyword_text']}"
            )
            _add(
                repository,
                provider,
                _candidate(
                    identity=f"security-{scenario}-{index}",
                    source_key=f"policy/security/{scenario}/{index}",
                    title=f"Security fixture {scenario} {index}",
                    content=content,
                    **overrides,
                ),
            )


def _relevance_cases(dataset: dict[str, object]) -> list[dict[str, str]]:
    policies = dataset["policies"]
    repetitions = int(dataset["repetitions_per_query_type"])
    assert isinstance(policies, list)
    cases: list[dict[str, str]] = []
    for policy in policies:
        assert isinstance(policy, dict)
        expected = f"policy/evaluation/{policy['concept']}"
        for repetition in range(repetitions):
            cases.extend(
                (
                    {
                        "id": f"{policy['concept']}-lexical-{repetition}",
                        "kind": "lexical_only",
                        "query": str(policy["code"]),
                        "expected": expected,
                    },
                    {
                        "id": f"{policy['concept']}-semantic-{repetition}",
                        "kind": "semantic_only",
                        "query": str(policy["semantic_alias"]),
                        "expected": expected,
                    },
                    {
                        "id": f"{policy['concept']}-both-{repetition}",
                        "kind": "dual_channel",
                        "query": (
                            f"{policy['code']} {policy['semantic_alias']}"
                        ),
                        "expected": expected,
                    },
                )
            )
    return cases


def _query(
    text: str,
    *,
    mode: RetrievalMode,
    top_k: int,
    identity: str,
) -> KnowledgeSearchQuery:
    return KnowledgeSearchQuery(
        text=text,
        as_of=NOW,
        top_k=top_k,
        mode=mode,
        request_id=f"day13-eval-request-{identity}",
        trace_id=f"day13-eval-trace-{identity}",
    )


def _evaluate_mode(
    service: KnowledgeSearchService,
    cases: list[dict[str, str]],
    *,
    mode: RetrievalMode,
    top_k: int,
) -> dict[str, object]:
    found = 0
    reciprocal_rank = 0.0
    by_kind: dict[str, dict[str, int]] = {}
    for case in cases:
        result = service.search(
            AGENT,
            _query(
                case["query"],
                mode=mode,
                top_k=top_k,
                identity=f"{mode.value}-{case['id']}",
            ),
        )
        ranks = {
            hit.source_key: rank
            for rank, hit in enumerate(result.hits, start=1)
        }
        rank = ranks.get(case["expected"])
        found += int(rank is not None)
        reciprocal_rank += 0.0 if rank is None else 1.0 / rank
        bucket = by_kind.setdefault(case["kind"], {"cases": 0, "found": 0})
        bucket["cases"] += 1
        bucket["found"] += int(rank is not None)
    return {
        "cases": len(cases),
        f"recall_at_{top_k}_percent": round(found / len(cases) * 100, 2),
        "mrr": round(reciprocal_rank / len(cases), 4),
        "found": found,
        "by_query_kind": {
            kind: {
                **values,
                "recall_percent": round(
                    values["found"] / values["cases"] * 100,
                    2,
                ),
            }
            for kind, values in by_kind.items()
        },
    }


def _evaluate_security(
    service: KnowledgeSearchService,
    dataset: dict[str, object],
    *,
    top_k: int,
) -> dict[str, object]:
    counts = dataset["security_scenario_counts"]
    assert isinstance(counts, dict)
    checked = 0
    leaked = 0
    for mode in RetrievalMode:
        for scenario, raw_count in counts.items():
            for index in range(int(raw_count)):
                code = f"RF-SEC-{scenario.upper()}-{index:02d}"
                result = service.search(
                    AGENT,
                    _query(
                        code,
                        mode=mode,
                        top_k=top_k,
                        identity=f"security-{mode.value}-{scenario}-{index}",
                    ),
                )
                checked += 1
                leaked += sum(
                    hit.source_key.startswith("policy/security/")
                    for hit in result.hits
                )
    return {
        "checks": checked,
        "unauthorized_hits": leaked,
        "blocked_percent": round((checked - leaked) / checked * 100, 2),
    }


def run(dataset_path: Path) -> dict[str, object]:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    policies = dataset["policies"]
    assert isinstance(policies, list)
    provider = SyntheticSemanticProvider(policies)
    repository = _build_repository(dataset, provider)
    service = KnowledgeSearchService(repository, provider)
    cases = _relevance_cases(dataset)
    top_k = int(dataset["top_k"])
    modes = {
        mode.value: _evaluate_mode(
            service,
            cases,
            mode=mode,
            top_k=top_k,
        )
        for mode in RetrievalMode
    }
    recall_key = f"recall_at_{top_k}_percent"
    best_single = max(
        float(modes[RetrievalMode.KEYWORD.value][recall_key]),
        float(modes[RetrievalMode.SEMANTIC.value][recall_key]),
    )
    hybrid_recall = float(modes[RetrievalMode.HYBRID.value][recall_key])
    security = _evaluate_security(service, dataset, top_k=top_k)
    return {
        "report_id": "day13-hybrid-retrieval-v1",
        "dataset": {
            "dataset_id": dataset["dataset_id"],
            "synthetic": dataset["synthetic"],
            "relevance_case_count": len(cases),
            "security_case_count": sum(
                int(value)
                for value in dataset["security_scenario_counts"].values()
            ),
            "top_k": top_k,
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "repository": "deterministic in-memory evaluation adapter",
            "embedding_provider": provider.model,
            "paid_api_calls": 0,
        },
        "retrieval_modes": modes,
        "security_filters": security,
        "measured_value": {
            "hybrid_recall_at_5_percent": hybrid_recall,
            "hybrid_recall_lift_over_best_single_channel_points": round(
                hybrid_recall - best_single,
                2,
            ),
            "hybrid_mrr": modes[RetrievalMode.HYBRID.value]["mrr"],
            "unauthorized_retrieval_hits": security["unauthorized_hits"],
            "retrieval_runs_traced": len(repository.retrieval_runs),
        },
        "interpretation_limits": [
            "The corpus, queries, and embeddings are synthetic and deterministic.",
            "The report compares retrieval orchestration and does not claim production model quality.",
            "Real pgvector, PostgreSQL ranking, and persistence are verified separately by integration tests.",
            "Production relevance requires a labeled corpus sampled from real support traffic.",
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
