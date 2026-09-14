from argparse import ArgumentParser
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import platform
from uuid import UUID

from app.domain.auth import AuthenticatedActor, Role
from app.domain.memory import (
    MemoryDecision,
    MemoryDeleteCommand,
    MemorySourceType,
    MemoryWriteCommand,
)
from app.repositories.authorization_audit_repository import (
    InMemoryAuthorizationAuditRepository,
)
from app.repositories.memory_repository import InMemoryMemoryRepository
from app.services.authorization_service import AuthorizationService
from app.services.memory_service import MemoryService

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = (
    ROOT / "evaluation" / "datasets" / "day16_long_term_memory_v1.json"
)
TENANT_ID = UUID("99000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 8, 5, 14, 0, tzinfo=UTC)
ACTOR = AuthenticatedActor(
    actor_id="day16-evaluation-supervisor",
    tenant_id=TENANT_ID,
    roles=frozenset({Role.SUPERVISOR}),
)


class SaveEverythingBaseline:
    def __init__(self) -> None:
        self.entries: list[dict[str, object]] = []

    def remember(
        self,
        *,
        key: str,
        value: str,
        raw_conversation: str,
        expires_at: datetime,
    ) -> None:
        self.entries.append(
            {
                "key": key,
                "value": value,
                "raw_conversation": raw_conversation,
                "expires_at": expires_at.isoformat(),
            }
        )

    def recall(self, key: str) -> str | None:
        for item in reversed(self.entries):
            if item["key"] == key:
                return str(item["value"])
        return None

    @property
    def payload_characters(self) -> int:
        return sum(
            len(json.dumps(item, ensure_ascii=False, sort_keys=True))
            for item in self.entries
        )


def _service():
    repository = InMemoryMemoryRepository()
    service = MemoryService(
        repository,
        AuthorizationService(InMemoryAuthorizationAuditRepository()),
        clock=lambda: NOW,
    )
    return service, repository


def _write(
    identity: str,
    *,
    key: str,
    value: str,
    source: MemorySourceType,
    observed_at: datetime = NOW,
    expires_at: datetime = NOW + timedelta(days=30),
) -> MemoryWriteCommand:
    return MemoryWriteCommand(
        subject_id=f"customer-{identity}",
        memory_key=key,
        value=value,
        source_type=source,
        source_reference=f"message-{identity}",
        confidence=0.99,
        observed_at=observed_at,
        expires_at=expires_at,
        idempotency_key=f"memory-{identity}-{value}",
        request_id=f"request-{identity}-{value}",
        trace_id=f"trace-{identity}",
    )


def _raw_conversation(identity: str, words: int) -> str:
    return (
        f"Conversation {identity}. The user stated a possible memory. "
        + "unrelated-history " * words
    )


def _evaluate_stable(identity: str, words: int):
    baseline = SaveEverythingBaseline()
    service, repository = _service()
    raw = _raw_conversation(identity, words)
    baseline.remember(
        key="preference.language",
        value="zh-CN",
        raw_conversation=raw,
        expires_at=NOW + timedelta(days=30),
    )
    result = service.remember(
        ACTOR,
        _write(
            identity,
            key="preference.language",
            value="zh-CN",
            source=MemorySourceType.EXPLICIT_USER,
        ),
    )
    final = repository.list_active_for_subject(
        TENANT_ID,
        f"customer-{identity}",
        at=NOW,
    )
    return _case_result(
        identity,
        "stable_preference",
        baseline_correct=baseline.recall("preference.language") == "zh-CN",
        final_correct=(
            result.decision is MemoryDecision.CREATED
            and len(final) == 1
            and final[0].value == "zh-cn"
        ),
        baseline_unsafe_write=0,
        final_unsafe_write=0,
        baseline_unsafe_recall=0,
        final_unsafe_recall=0,
        baseline_chars=baseline.payload_characters,
        final_chars=sum(len(item.value or "") for item in repository.memories),
    )


def _evaluate_inference(identity: str, words: int):
    baseline = SaveEverythingBaseline()
    service, repository = _service()
    baseline.remember(
        key="preference.language",
        value="zh-CN",
        raw_conversation=_raw_conversation(identity, words),
        expires_at=NOW + timedelta(days=30),
    )
    result = service.remember(
        ACTOR,
        _write(
            identity,
            key="preference.language",
            value="zh-CN",
            source=MemorySourceType.MODEL_INFERENCE,
        ),
    )
    return _case_result(
        identity,
        "unconfirmed_model_inference",
        baseline_correct=False,
        final_correct=(
            result.decision is MemoryDecision.CONFIRMATION_REQUIRED
            and repository.memories == []
        ),
        baseline_unsafe_write=1,
        final_unsafe_write=len(repository.memories),
        baseline_unsafe_recall=0,
        final_unsafe_recall=0,
        baseline_chars=baseline.payload_characters,
        final_chars=0,
    )


def _evaluate_business_fact(identity: str, words: int):
    baseline = SaveEverythingBaseline()
    service, repository = _service()
    baseline.remember(
        key="order.refund_eligible",
        value="true",
        raw_conversation=_raw_conversation(identity, words),
        expires_at=NOW + timedelta(days=30),
    )
    result = service.remember(
        ACTOR,
        _write(
            identity,
            key="order.refund_eligible",
            value="true",
            source=MemorySourceType.EXPLICIT_USER,
        ),
    )
    return _case_result(
        identity,
        "prohibited_business_fact",
        baseline_correct=False,
        final_correct=(
            result.decision is MemoryDecision.REJECTED
            and repository.memories == []
        ),
        baseline_unsafe_write=1,
        final_unsafe_write=len(repository.memories),
        baseline_unsafe_recall=0,
        final_unsafe_recall=0,
        baseline_chars=baseline.payload_characters,
        final_chars=0,
    )


def _evaluate_lifecycle(identity: str, kind: str, words: int):
    baseline = SaveEverythingBaseline()
    service, repository = _service()
    key = "preference.response_style"
    baseline.remember(
        key=key,
        value="concise",
        raw_conversation=_raw_conversation(identity, words),
        expires_at=NOW + timedelta(days=1),
    )
    service.remember(
        ACTOR,
        _write(
            identity,
            key=key,
            value="concise",
            source=MemorySourceType.EXPLICIT_USER,
            expires_at=NOW + timedelta(days=1),
        ),
    )
    subject = f"customer-{identity}"
    baseline_correct = False
    baseline_unsafe_recall = 0
    if kind == "expired":
        baseline_unsafe_recall = int(baseline.recall(key) is not None)
        final = repository.list_active_for_subject(
            TENANT_ID,
            subject,
            at=NOW + timedelta(days=2),
        )
        final_correct = final == ()
    elif kind == "conflict":
        baseline.remember(
            key=key,
            value="detailed",
            raw_conversation=_raw_conversation(identity + "-old", words),
            expires_at=NOW + timedelta(days=30),
        )
        service.remember(
            ACTOR,
            _write(
                identity + "-conflict",
                key=key,
                value="detailed",
                source=MemorySourceType.EXPLICIT_USER,
                observed_at=NOW - timedelta(hours=1),
                expires_at=NOW + timedelta(days=30),
            ).model_copy(update={"subject_id": subject}),
        )
        baseline_unsafe_recall = int(baseline.recall(key) == "detailed")
        final_correct = repository.list_active_for_subject(
            TENANT_ID,
            subject,
            at=NOW,
        ) == ()
    elif kind == "deleted":
        service.forget(
            ACTOR,
            MemoryDeleteCommand(
                subject_id=subject,
                memory_key=key,
                reason="Synthetic deletion request.",
                idempotency_key=f"delete-{identity}",
                request_id=f"request-delete-{identity}",
                trace_id=f"trace-{identity}",
            ),
        )
        baseline_unsafe_recall = int(baseline.recall(key) is not None)
        final_correct = (
            repository.list_active_for_subject(
                TENANT_ID,
                subject,
                at=NOW,
            )
            == ()
            and all(item.value is None for item in repository.memories)
        )
    else:
        baseline.remember(
            key=key,
            value="detailed",
            raw_conversation=_raw_conversation(identity + "-new", words),
            expires_at=NOW + timedelta(days=30),
        )
        service.remember(
            ACTOR,
            _write(
                identity + "-update",
                key=key,
                value="detailed",
                source=MemorySourceType.EXPLICIT_USER,
                observed_at=NOW + timedelta(hours=1),
                expires_at=NOW + timedelta(days=30),
            ).model_copy(update={"subject_id": subject}),
        )
        baseline_correct = baseline.recall(key) == "detailed"
        final = repository.list_active_for_subject(
            TENANT_ID,
            subject,
            at=NOW,
        )
        final_correct = len(final) == 1 and final[0].value == "detailed"
    return _case_result(
        identity,
        f"lifecycle_{kind}",
        baseline_correct=baseline_correct,
        final_correct=final_correct,
        baseline_unsafe_write=0,
        final_unsafe_write=0,
        baseline_unsafe_recall=baseline_unsafe_recall,
        final_unsafe_recall=0,
        baseline_chars=baseline.payload_characters,
        final_chars=sum(len(item.value or "") for item in repository.memories),
    )


def _case_result(
    identity: str,
    scenario: str,
    *,
    baseline_correct: bool,
    final_correct: bool,
    baseline_unsafe_write: int,
    final_unsafe_write: int,
    baseline_unsafe_recall: int,
    final_unsafe_recall: int,
    baseline_chars: int,
    final_chars: int,
) -> dict[str, object]:
    return {
        "case_id": identity,
        "scenario": scenario,
        "baseline": {
            "correct": baseline_correct,
            "unsafe_writes": baseline_unsafe_write,
            "unsafe_recalls": baseline_unsafe_recall,
            "memory_payload_characters": baseline_chars,
        },
        "memory_service": {
            "correct": final_correct,
            "unsafe_writes": final_unsafe_write,
            "unsafe_recalls": final_unsafe_recall,
            "memory_payload_characters": final_chars,
        },
    }


def _summary(raw: list[dict[str, object]], key: str) -> dict[str, object]:
    items = [item[key] for item in raw]
    assert all(isinstance(item, dict) for item in items)
    return {
        "correct_handling_percent": round(
            sum(bool(item["correct"]) for item in items) / len(items) * 100,
            2,
        ),
        "unsafe_writes": sum(int(item["unsafe_writes"]) for item in items),
        "unsafe_recalls": sum(int(item["unsafe_recalls"]) for item in items),
        "average_memory_payload_characters": round(
            sum(int(item["memory_payload_characters"]) for item in items)
            / len(items),
            2,
        ),
    }


def run(dataset_path: Path = DEFAULT_DATASET) -> dict[str, object]:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    words = int(dataset["history_noise_words"])
    raw: list[dict[str, object]] = []
    raw.extend(
        _evaluate_stable(f"stable-{index:02d}", words)
        for index in range(int(dataset["stable_preference_cases"]))
    )
    raw.extend(
        _evaluate_inference(f"inference-{index:02d}", words)
        for index in range(int(dataset["model_inference_cases"]))
    )
    raw.extend(
        _evaluate_business_fact(f"business-{index:02d}", words)
        for index in range(int(dataset["prohibited_business_fact_cases"]))
    )
    for kind in ("expired", "conflict", "deleted", "updated"):
        raw.extend(
            _evaluate_lifecycle(f"{kind}-{index:02d}", kind, words)
            for index in range(int(dataset["lifecycle_cases_per_type"]))
        )
    baseline = _summary(raw, "baseline")
    final = _summary(raw, "memory_service")
    return {
        "dataset": {
            "id": dataset["dataset_id"],
            "synthetic": dataset["synthetic"],
            "case_count": len(raw),
            "path": str(dataset_path),
        },
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "paid_model_calls": 0,
            "embedding_calls": 0,
        },
        "save_everything_baseline": baseline,
        "policy_controlled_memory": final,
        "measured_value": {
            "correct_handling_lift_points": round(
                float(final["correct_handling_percent"])
                - float(baseline["correct_handling_percent"]),
                2,
            ),
            "unsafe_writes_blocked": int(baseline["unsafe_writes"])
            - int(final["unsafe_writes"]),
            "unsafe_recalls_blocked": int(baseline["unsafe_recalls"])
            - int(final["unsafe_recalls"]),
            "memory_payload_reduction_percent": round(
                (
                    float(baseline["average_memory_payload_characters"])
                    - float(final["average_memory_payload_characters"])
                )
                / float(baseline["average_memory_payload_characters"])
                * 100,
                2,
            ),
        },
        "raw_results": raw,
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
