from argparse import ArgumentParser
import json
from pathlib import Path
import platform
from uuid import NAMESPACE_URL, UUID, uuid5

from app.context_engineering.builder import ContextBuilder
from app.context_engineering.tokens import TiktokenTokenCounter
from app.domain.context import (
    ContextBuildRequest,
    ContextDecisionReason,
    ContextFragmentCandidate,
    ContextSource,
    ContextTrustLevel,
    ContextWindowBudget,
)
from app.repositories.context_trace_repository import (
    InMemoryContextTraceRepository,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = (
    ROOT / "evaluation" / "datasets" / "day15_context_builder_v1.json"
)
TENANT_ID = UUID("93000000-0000-0000-0000-000000000001")
MODEL = "gpt-5.6-terra"


def _candidate(
    identity: str,
    source: ContextSource,
    content,
    *,
    semantic_key: str | None = None,
    priority: int,
    relevance: int,
    ordinal: int = 0,
    required: bool = False,
    replaceable: bool = False,
    eligible: bool = True,
    exclusion_reason: ContextDecisionReason | None = None,
) -> ContextFragmentCandidate:
    if source is ContextSource.SYSTEM_POLICY:
        trust = ContextTrustLevel.SYSTEM
    elif source in {
        ContextSource.AGENT_STATE,
        ContextSource.CONFIRMED_FACT,
        ContextSource.TOOL_DESCRIPTOR,
    }:
        trust = ContextTrustLevel.VERIFIED_INTERNAL
    else:
        trust = ContextTrustLevel.USER_PROVIDED
    return ContextFragmentCandidate(
        fragment_id=identity,
        semantic_key=semantic_key or identity,
        source=source,
        trust_level=trust,
        content=content,
        priority=priority,
        relevance=relevance,
        ordinal=ordinal,
        required=required,
        replaceable=replaceable,
        eligible=eligible,
        exclusion_reason=exclusion_reason,
    )


def _common(case_id: str) -> list[ContextFragmentCandidate]:
    return [
        _candidate(
            f"{case_id}:system",
            ContextSource.SYSTEM_POLICY,
            "System safety policy must always remain in context.",
            priority=100,
            relevance=100,
            required=True,
        ),
        _candidate(
            f"{case_id}:goal",
            ContextSource.CURRENT_GOAL,
            {"order_id": "10087", "goal": "resolve delivery dispute"},
            priority=100,
            relevance=100,
            required=True,
        ),
    ]


def _cases(dataset: dict[str, object]) -> list[dict[str, object]]:
    count = int(dataset["cases_per_scenario"])
    history_count = int(dataset["long_history_fragments"])
    words = int(dataset["long_history_words_per_fragment"])
    cases: list[dict[str, object]] = []
    for index in range(count):
        case_id = f"long-{index:02d}"
        candidates = _common(case_id)
        candidates.extend(
            _candidate(
                f"{case_id}:history:{item}",
                ContextSource.HISTORY,
                f"irrelevant-{item} " + "noise " * words,
                priority=10,
                relevance=30,
                ordinal=item,
            )
            for item in range(history_count)
        )
        candidates.extend(
            (
                _candidate(
                    f"{case_id}:correction",
                    ContextSource.USER_CORRECTION,
                    {"authoritative_order_id": "10087"},
                    priority=99,
                    relevance=100,
                    ordinal=100,
                ),
                _candidate(
                    f"{case_id}:evidence",
                    ContextSource.CONFIRMED_FACT,
                    {"delivery_proof": "not_available"},
                    priority=98,
                    relevance=100,
                    ordinal=101,
                ),
            )
        )
        cases.append(
            {
                "id": case_id,
                "scenario": "long_history",
                "candidates": tuple(candidates),
                "critical": {
                    f"{case_id}:system",
                    f"{case_id}:goal",
                    f"{case_id}:correction",
                    f"{case_id}:evidence",
                },
                "stale": set(),
                "relevant_tools": set(),
            }
        )

    for index in range(count):
        case_id = f"correction-{index:02d}"
        candidates = _common(case_id)
        candidates.extend(
            (
                _candidate(
                    f"{case_id}:old",
                    ContextSource.USER_CORRECTION,
                    {"authoritative_order_id": "10086"},
                    semantic_key=f"{case_id}:order",
                    priority=99,
                    relevance=100,
                    ordinal=1,
                    replaceable=True,
                ),
                _candidate(
                    f"{case_id}:latest",
                    ContextSource.USER_CORRECTION,
                    {"authoritative_order_id": "10087"},
                    semantic_key=f"{case_id}:order",
                    priority=99,
                    relevance=100,
                    ordinal=2,
                    replaceable=True,
                ),
                _candidate(
                    f"{case_id}:stale-evidence",
                    ContextSource.TOOL_OBSERVATION,
                    {"order_id": "10086", "status": "delivered"},
                    priority=95,
                    relevance=0,
                    eligible=False,
                    exclusion_reason=ContextDecisionReason.INVALIDATED,
                ),
                _candidate(
                    f"{case_id}:current-evidence",
                    ContextSource.CONFIRMED_FACT,
                    {"order_id": "10087", "status": "shipped"},
                    priority=95,
                    relevance=100,
                ),
            )
        )
        cases.append(
            {
                "id": case_id,
                "scenario": "latest_correction",
                "candidates": tuple(candidates),
                "critical": {
                    f"{case_id}:system",
                    f"{case_id}:goal",
                    f"{case_id}:latest",
                    f"{case_id}:current-evidence",
                },
                "stale": {
                    f"{case_id}:old",
                    f"{case_id}:stale-evidence",
                },
                "relevant_tools": set(),
            }
        )

    for index in range(count):
        case_id = f"tools-{index:02d}"
        candidates = _common(case_id)
        relevant = ("order_lookup", "logistics_lookup", "policy_lookup")[
            index % 3
        ]
        for tool_name in ("order_lookup", "logistics_lookup", "policy_lookup"):
            candidates.append(
                _candidate(
                    f"{case_id}:tool:{tool_name}",
                    ContextSource.TOOL_DESCRIPTOR,
                    {"allowed_tools": [{"name": tool_name}]},
                    priority=80,
                    relevance=100 if tool_name == relevant else 0,
                    eligible=tool_name == relevant,
                    exclusion_reason=(
                        None
                        if tool_name == relevant
                        else ContextDecisionReason.NOT_RELEVANT_TO_CURRENT_STEP
                    ),
                )
            )
        cases.append(
            {
                "id": case_id,
                "scenario": "dynamic_tools",
                "candidates": tuple(candidates),
                "critical": {
                    f"{case_id}:system",
                    f"{case_id}:goal",
                    f"{case_id}:tool:{relevant}",
                },
                "stale": set(),
                "relevant_tools": {f"{case_id}:tool:{relevant}"},
            }
        )
    return cases


def _baseline_select(
    candidates: tuple[ContextFragmentCandidate, ...],
    counter: TiktokenTokenCounter,
    budget: int,
) -> tuple[set[str], int]:
    """Naive baseline: include all eligible or stale data, keep tail on overflow."""
    selected: list[ContextFragmentCandidate] = list(candidates)
    while _measure(selected, counter) > budget:
        removable = next(
            (item for item in selected if item.source is not ContextSource.SYSTEM_POLICY),
            None,
        )
        if removable is None:
            break
        selected.remove(removable)
    return {item.fragment_id for item in selected}, _measure(selected, counter)


def _measure(
    selected: list[ContextFragmentCandidate],
    counter: TiktokenTokenCounter,
) -> int:
    instructions = "\n\n".join(
        str(item.content)
        for item in selected
        if item.source is ContextSource.SYSTEM_POLICY
    )
    runtime = {
        "context_fragments": [
            {
                "fragment_id": item.fragment_id,
                "semantic_key": item.semantic_key,
                "source": item.source.value,
                "trust_level": item.trust_level.value,
                "content": item.content,
            }
            for item in selected
            if item.source is not ContextSource.SYSTEM_POLICY
        ]
    }
    input_text = (
        "Choose the next action from this runtime snapshot.\n"
        "<agent_runtime_data>"
        + json.dumps(
            runtime,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "</agent_runtime_data>"
    )
    return counter.count(instructions) + counter.count(input_text)


def _summarize(raw: list[dict[str, object]]) -> dict[str, object]:
    total = len(raw)
    retained = sum(bool(item["critical_retained"]) for item in raw)
    stale = sum(int(item["stale_selected"]) for item in raw)
    selected_tools = sum(int(item["selected_tools"]) for item in raw)
    correct_tools = sum(int(item["correct_tools"]) for item in raw)
    return {
        "case_count": total,
        "critical_fragment_retention_percent": round(retained / total * 100, 2),
        "stale_fragment_selections": stale,
        "dynamic_tool_precision_percent": round(
            (correct_tools / selected_tools * 100) if selected_tools else 100.0,
            2,
        ),
        "average_input_tokens": round(
            sum(int(item["input_tokens"]) for item in raw) / total,
            2,
        ),
    }


def run(dataset_path: Path = DEFAULT_DATASET) -> dict[str, object]:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    counter = TiktokenTokenCounter(MODEL)
    budget = ContextWindowBudget(
        context_window_tokens=int(dataset["context_window_tokens"]),
        reserved_output_tokens=int(dataset["reserved_output_tokens"]),
        reserved_reasoning_tokens=int(dataset["reserved_reasoning_tokens"]),
        safety_margin_tokens=int(dataset["safety_margin_tokens"]),
        max_input_tokens=int(dataset["max_input_tokens"]),
    )
    baseline_raw: list[dict[str, object]] = []
    optimized_raw: list[dict[str, object]] = []
    for case in _cases(dataset):
        candidates = case["candidates"]
        assert isinstance(candidates, tuple)
        baseline_ids, baseline_tokens = _baseline_select(
            candidates,
            counter,
            budget.input_budget_tokens,
        )
        repository = InMemoryContextTraceRepository()
        result = ContextBuilder(counter, repository).build(
            ContextBuildRequest(
                tenant_id=TENANT_ID,
                agent_run_id=uuid5(NAMESPACE_URL, f"day15:{case['id']}"),
                step_number=1,
                model=MODEL,
                budget=budget,
                candidates=candidates,
                request_id=f"day15-request-{case['id']}",
                trace_id=f"day15-trace-{case['id']}",
            )
        )
        optimized_ids = {
            item.fragment_id for item in result.included_fragments
        }
        for target, selected_ids, tokens in (
            (baseline_raw, baseline_ids, baseline_tokens),
            (optimized_raw, optimized_ids, result.run.actual_input_tokens),
        ):
            critical = case["critical"]
            stale = case["stale"]
            tools = {
                item.fragment_id
                for item in candidates
                if item.source is ContextSource.TOOL_DESCRIPTOR
                and item.fragment_id in selected_ids
            }
            relevant_tools = case["relevant_tools"]
            assert isinstance(critical, set)
            assert isinstance(stale, set)
            assert isinstance(relevant_tools, set)
            target.append(
                {
                    "case_id": case["id"],
                    "scenario": case["scenario"],
                    "critical_retained": critical <= selected_ids,
                    "stale_selected": len(stale & selected_ids),
                    "selected_tools": len(tools),
                    "correct_tools": len(tools & relevant_tools),
                    "input_tokens": tokens,
                    "selected_fragment_ids": sorted(selected_ids),
                }
            )

    baseline = _summarize(baseline_raw)
    optimized = _summarize(optimized_raw)
    return {
        "dataset": {
            "id": dataset["dataset_id"],
            "synthetic": dataset["synthetic"],
            "case_count": len(baseline_raw),
            "path": str(dataset_path),
        },
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "tokenizer": "tiktoken-0.13.0/o200k_base-fallback",
            "model": MODEL,
            "input_budget_tokens": budget.input_budget_tokens,
            "paid_model_calls": 0,
        },
        "baseline": baseline,
        "context_builder": optimized,
        "measured_value": {
            "critical_retention_lift_points": round(
                float(optimized["critical_fragment_retention_percent"])
                - float(baseline["critical_fragment_retention_percent"]),
                2,
            ),
            "stale_selections_removed": int(
                baseline["stale_fragment_selections"]
            )
            - int(optimized["stale_fragment_selections"]),
            "tool_precision_lift_points": round(
                float(optimized["dynamic_tool_precision_percent"])
                - float(baseline["dynamic_tool_precision_percent"]),
                2,
            ),
            "average_input_token_change_percent": round(
                (
                    float(optimized["average_input_tokens"])
                    - float(baseline["average_input_tokens"])
                )
                / float(baseline["average_input_tokens"])
                * 100,
                2,
            ),
        },
        "raw_results": {
            "baseline": baseline_raw,
            "context_builder": optimized_raw,
        },
    }


def main() -> None:
    parser = ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run(args.dataset)
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)


if __name__ == "__main__":
    main()
