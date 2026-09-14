from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import re
from time import perf_counter
from typing import Callable, Protocol
from uuid import uuid4

from opentelemetry.trace import SpanKind

from app.core.errors import AuthorizationDeniedError
from app.domain.auth import AuthenticatedActor, Permission
from app.domain.grounded_answer import (
    DraftDisposition,
    EvidenceAssessmentItem,
    EvidenceConflictPair,
    EvidenceRelation,
    EvidenceRerankOutput,
    GroundedAnswerDraft,
    GroundedAnswerQuery,
    GroundedAnswerResult,
    GroundedCitation,
    GroundedClaim,
    KnowledgeAnswerRun,
    KnowledgeAnswerCitationTrace,
    KnowledgeAnswerStatus,
)
from app.domain.retrieval import (
    KnowledgeSearchHit,
    KnowledgeSearchQuery,
    KnowledgeSearchResult,
)
from app.knowledge.answering_errors import (
    EvidenceAssessmentContractError,
    GroundingVerificationError,
    KnowledgeAnsweringError,
    KnowledgeAnswerRecordingError,
)
from app.knowledge.answering_prompts import (
    ANSWER_PROMPT_NAME,
    ANSWER_PROMPT_VERSION,
    RERANK_PROMPT_NAME,
    RERANK_PROMPT_VERSION,
    create_knowledge_answer_prompt_registry,
)
from app.knowledge.retrieval_errors import KnowledgeRetrievalError
from app.llm.contracts import StructuredModelRequest
from app.llm.errors import ModelGatewayError
from app.llm.prompts import PromptRegistry
from app.repositories.knowledge_answer_repository import (
    KnowledgeAnswerRepository,
)
from app.security.content import UntrustedContentGuard
from app.observability.tracing import (
    FailureDomain,
    add_safe_event,
    mark_span_error,
    operation_span,
    set_safe_attributes,
)
from app.observability.metrics import get_metrics

INSUFFICIENT_ANSWER = (
    "当前没有足够的有效政策证据，无法给出确定结论。"
)
CONFLICT_ANSWER = (
    "当前有效政策证据存在冲突，无法给出确定结论，需要人工核对。"
)
SECURITY_BLOCKED_ANSWER = (
    "当前请求包含不能安全用于政策检索的指令，已停止自动处理。"
)
_CITATION_MARKER = re.compile(r"\[K\d+\]")
_WHITESPACE = re.compile(r"\s+")


class KnowledgeSearcher(Protocol):
    def search(
        self,
        actor: AuthenticatedActor,
        query: KnowledgeSearchQuery,
    ) -> KnowledgeSearchResult: ...


class StructuredGenerator(Protocol):
    def generate(self, request: StructuredModelRequest): ...


@dataclass(frozen=True, slots=True)
class _Evidence:
    evidence_id: str
    initial_rank: int
    hit: KnowledgeSearchHit


class GroundedAnswerService:
    def __init__(
        self,
        searcher: KnowledgeSearcher,
        model_gateway: StructuredGenerator,
        repository: KnowledgeAnswerRepository,
        *,
        prompt_registry: PromptRegistry | None = None,
        security_guard: UntrustedContentGuard | None = None,
        min_relevance: int = 60,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic_clock: Callable[[], float] = perf_counter,
    ) -> None:
        if not 0 <= min_relevance <= 100:
            raise ValueError("min_relevance must be between 0 and 100.")
        self._searcher = searcher
        self._model_gateway = model_gateway
        self._repository = repository
        self._prompt_registry = (
            prompt_registry or create_knowledge_answer_prompt_registry()
        )
        self._security_guard = security_guard or UntrustedContentGuard()
        self._min_relevance = min_relevance
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock

    def answer(
        self,
        actor: AuthenticatedActor,
        query: GroundedAnswerQuery,
    ) -> GroundedAnswerResult:
        with operation_span(
            "ground knowledge answer",
            kind=SpanKind.INTERNAL,
            failure_domain=FailureDomain.BUSINESS,
            attributes={
                "resolveflow.component": "rag",
                "resolveflow.rag.query_hash": _hash_text(query.question),
                "resolveflow.rag.retrieval_mode": query.retrieval_mode.value,
                "resolveflow.rag.candidate_k": query.candidate_k,
                "resolveflow.rag.max_evidence": query.max_evidence,
                "resolveflow.request_id": query.request_id,
                "resolveflow.trace_id": query.trace_id,
            },
        ) as span:
            try:
                result = self._answer(actor, query)
            except AuthorizationDeniedError as exc:
                get_metrics().record_rag(status="failed", failure_domain="security")
                mark_span_error(
                    span,
                    FailureDomain.SECURITY,
                    type(exc).__name__,
                    retryable=False,
                )
                raise
            except KnowledgeAnswerRecordingError as exc:
                get_metrics().record_rag(status="failed", failure_domain="database")
                mark_span_error(
                    span,
                    FailureDomain.DATABASE,
                    exc.code,
                    retryable=exc.retryable,
                )
                raise
            except KnowledgeRetrievalError as exc:
                get_metrics().record_rag(status="failed", failure_domain="retrieval")
                mark_span_error(
                    span,
                    FailureDomain.RETRIEVAL,
                    exc.code,
                    retryable=exc.retryable,
                )
                raise
            except ModelGatewayError as exc:
                get_metrics().record_rag(status="failed", failure_domain="model")
                mark_span_error(
                    span,
                    FailureDomain.MODEL,
                    exc.error_code,
                )
                raise
            except KnowledgeAnsweringError as exc:
                get_metrics().record_rag(status="failed", failure_domain="policy")
                mark_span_error(
                    span,
                    FailureDomain.POLICY,
                    exc.code,
                    retryable=exc.retryable,
                )
                raise
            set_safe_attributes(
                span,
                {
                    "resolveflow.rag.run_id": result.run.id,
                    "resolveflow.rag.status": result.status.value,
                    "resolveflow.rag.retrieved_candidates": (
                        result.run.retrieved_candidate_count
                    ),
                    "resolveflow.rag.eligible_candidates": (
                        result.run.eligible_candidate_count
                    ),
                    "resolveflow.rag.selected_candidates": (
                        result.run.selected_candidate_count
                    ),
                    "resolveflow.rag.conflict_count": (
                        result.run.conflict_count
                    ),
                    "resolveflow.rag.citation_count": (
                        result.run.citation_count
                    ),
                    "resolveflow.rag.latency_ms": result.run.latency_ms,
                },
            )
            get_metrics().record_rag(
                status=result.status.value,
                failure_domain=(
                    "security"
                    if result.status is KnowledgeAnswerStatus.SECURITY_BLOCKED
                    else "none"
                ),
            )
            if result.status is KnowledgeAnswerStatus.SECURITY_BLOCKED:
                mark_span_error(
                    span,
                    FailureDomain.SECURITY,
                    "knowledge_query_security_blocked",
                    retryable=False,
                )
            elif result.status in {
                KnowledgeAnswerStatus.INSUFFICIENT_EVIDENCE,
                KnowledgeAnswerStatus.EVIDENCE_CONFLICT,
            }:
                add_safe_event(
                    span,
                    "rag.safe_abstention",
                    {"status": result.status.value},
                )
            return result

    def _answer(
        self,
        actor: AuthenticatedActor,
        query: GroundedAnswerQuery,
    ) -> GroundedAnswerResult:
        if not actor.has_permission(Permission.TOOL_POLICY_READ):
            raise AuthorizationDeniedError(Permission.TOOL_POLICY_READ.value)
        started_tick = self._monotonic_clock()
        run = KnowledgeAnswerRun(
            id=uuid4(),
            tenant_id=actor.tenant_id,
            actor_id=actor.actor_id,
            actor_roles=tuple(
                sorted(actor.roles, key=lambda role: role.value)
            ),
            query_hash=_hash_text(query.question),
            retrieval_run_id=None,
            status=KnowledgeAnswerStatus.PROCESSING,
            retrieved_candidate_count=0,
            eligible_candidate_count=0,
            deduplicated_candidate_count=0,
            selected_candidate_count=0,
            conflict_count=0,
            citation_count=0,
            rerank_call_id=None,
            answer_call_id=None,
            answer_hash=None,
            error_code=None,
            latency_ms=0,
            request_id=query.request_id,
            trace_id=query.trace_id,
            created_at=self._wall_clock(),
            completed_at=None,
        )
        self._add_run(run)
        try:
            protected_question = self._security_guard.inspect_text(
                query.question
            )
            if protected_question.prompt_injection_detected:
                return self._safe_result(
                    run,
                    status=KnowledgeAnswerStatus.SECURITY_BLOCKED,
                    answer_text=SECURITY_BLOCKED_ANSWER,
                    started_tick=started_tick,
                )
            safe_question = protected_question.value
            retrieval = self._searcher.search(
                actor,
                KnowledgeSearchQuery(
                    text=safe_question,
                    as_of=query.as_of,
                    top_k=query.candidate_k,
                    mode=query.retrieval_mode,
                    request_id=query.request_id,
                    trace_id=query.trace_id,
                ),
            )
            eligible_hits = _security_eligible_hits(
                _eligible_hits(retrieval.hits, query.as_of),
                self._security_guard,
            )
            evidence = _deduplicate_evidence(eligible_hits)
            run = run.model_copy(
                update={
                    "retrieval_run_id": retrieval.run.id,
                    "retrieved_candidate_count": len(retrieval.hits),
                    "eligible_candidate_count": len(eligible_hits),
                    "deduplicated_candidate_count": len(evidence),
                }
            )
            if not evidence:
                return self._safe_result(
                    run,
                    status=KnowledgeAnswerStatus.INSUFFICIENT_EVIDENCE,
                    answer_text=INSUFFICIENT_ANSWER,
                    started_tick=started_tick,
                )

            rerank_response = self._rerank(run, safe_question, evidence)
            run = run.model_copy(
                update={"rerank_call_id": rerank_response.call_id}
            )
            assessment_by_id = _validate_assessment(
                rerank_response.output,
                evidence,
            )
            relevant = [
                item
                for item in evidence
                if (
                    assessment_by_id[item.evidence_id].relation
                    is not EvidenceRelation.IRRELEVANT
                    and assessment_by_id[item.evidence_id].relevance
                    >= self._min_relevance
                )
            ]
            relevant.sort(
                key=lambda item: (
                    -assessment_by_id[item.evidence_id].relevance,
                    item.initial_rank,
                )
            )
            selected = relevant[: query.max_evidence]
            run = run.model_copy(
                update={"selected_candidate_count": len(selected)}
            )
            if not selected:
                return self._safe_result(
                    run,
                    status=KnowledgeAnswerStatus.INSUFFICIENT_EVIDENCE,
                    answer_text=INSUFFICIENT_ANSWER,
                    started_tick=started_tick,
                )

            conflicts = _relevant_conflicts(
                rerank_response.output.conflicts,
                relevant,
            )
            conflicts.update(_version_conflicts(relevant))
            if conflicts:
                conflict_evidence = _conflict_evidence(relevant, conflicts)
                citations = tuple(
                    _citation_for_evidence(
                        item,
                        item.hit.content[:1_000],
                    )
                    for item in conflict_evidence
                )
                run = run.model_copy(
                    update={"conflict_count": len(conflicts)}
                )
                return self._safe_result(
                    run,
                    status=KnowledgeAnswerStatus.EVIDENCE_CONFLICT,
                    answer_text=CONFLICT_ANSWER,
                    citations=citations,
                    started_tick=started_tick,
                )

            answer_response = self._draft_answer(
                run,
                safe_question,
                selected,
            )
            run = run.model_copy(
                update={"answer_call_id": answer_response.call_id}
            )
            if (
                answer_response.output.disposition
                is DraftDisposition.INSUFFICIENT_EVIDENCE
            ):
                return self._safe_result(
                    run,
                    status=KnowledgeAnswerStatus.INSUFFICIENT_EVIDENCE,
                    answer_text=INSUFFICIENT_ANSWER,
                    started_tick=started_tick,
                )
            claims, citations = _verify_and_render(
                answer_response.output,
                selected,
                self._security_guard,
            )
            answer_text = "\n".join(
                f"{claim.text} "
                + "".join(
                    f"[{citation_id}]"
                    for citation_id in claim.citation_ids
                )
                for claim in claims
            )
            return self._safe_result(
                run,
                status=KnowledgeAnswerStatus.ANSWERED,
                answer_text=answer_text,
                claims=claims,
                citations=citations,
                started_tick=started_tick,
            )
        except KnowledgeAnswerRecordingError:
            raise
        except Exception as exc:
            self._fail_run(
                run,
                error_code=_error_code(exc),
                started_tick=started_tick,
            )
            raise

    def _rerank(
        self,
        run: KnowledgeAnswerRun,
        question: str,
        evidence: list[_Evidence],
    ):
        prompt = self._prompt_registry.get(
            RERANK_PROMPT_NAME,
            RERANK_PROMPT_VERSION,
        ).render(
            {
                "question": question,
                "evidence": _evidence_payload(evidence),
            }
        )
        return self._model_gateway.generate(
            StructuredModelRequest(
                tenant_id=run.tenant_id,
                operation="knowledge_evidence_rerank",
                prompt_name=prompt.name,
                prompt_version=prompt.version,
                prompt_hash=prompt.prompt_hash,
                resource_type="knowledge_answer",
                resource_id=str(run.id),
                instructions=prompt.instructions,
                input_text=prompt.input_text,
                response_model=EvidenceRerankOutput,
                request_id=run.request_id,
                trace_id=run.trace_id,
            )
        )

    def _draft_answer(
        self,
        run: KnowledgeAnswerRun,
        question: str,
        evidence: list[_Evidence],
    ):
        prompt = self._prompt_registry.get(
            ANSWER_PROMPT_NAME,
            ANSWER_PROMPT_VERSION,
        ).render(
            {
                "question": question,
                "evidence": _evidence_payload(evidence),
            }
        )
        return self._model_gateway.generate(
            StructuredModelRequest(
                tenant_id=run.tenant_id,
                operation="knowledge_grounded_answer",
                prompt_name=prompt.name,
                prompt_version=prompt.version,
                prompt_hash=prompt.prompt_hash,
                resource_type="knowledge_answer",
                resource_id=str(run.id),
                instructions=prompt.instructions,
                input_text=prompt.input_text,
                response_model=GroundedAnswerDraft,
                request_id=run.request_id,
                trace_id=run.trace_id,
            )
        )

    def _safe_result(
        self,
        run: KnowledgeAnswerRun,
        *,
        status: KnowledgeAnswerStatus,
        answer_text: str,
        claims: tuple[GroundedClaim, ...] = (),
        citations: tuple[GroundedCitation, ...] = (),
        started_tick: float,
    ) -> GroundedAnswerResult:
        completed = run.model_copy(
            update={
                "status": status,
                "citation_count": len(citations),
                "answer_hash": _hash_text(answer_text),
                "latency_ms": (
                    self._monotonic_clock() - started_tick
                )
                * 1_000,
                "completed_at": self._wall_clock(),
            }
        )
        traces = _citation_traces(completed.id, claims, citations)
        self._finish_run(completed, traces)
        return GroundedAnswerResult(
            run=completed,
            status=status,
            answer_text=answer_text,
            claims=claims,
            citations=citations,
        )

    def _fail_run(
        self,
        run: KnowledgeAnswerRun,
        *,
        error_code: str,
        started_tick: float,
    ) -> None:
        failed = run.model_copy(
            update={
                "status": KnowledgeAnswerStatus.FAILED,
                "error_code": error_code,
                "latency_ms": (
                    self._monotonic_clock() - started_tick
                )
                * 1_000,
                "completed_at": self._wall_clock(),
            }
        )
        self._finish_run(failed, ())

    def _add_run(self, run: KnowledgeAnswerRun) -> None:
        try:
            self._repository.add_run(run)
        except Exception as exc:
            raise KnowledgeAnswerRecordingError() from exc

    def _finish_run(
        self,
        run: KnowledgeAnswerRun,
        citations: tuple[KnowledgeAnswerCitationTrace, ...],
    ) -> None:
        try:
            self._repository.finish_run(run, citations)
        except Exception as exc:
            raise KnowledgeAnswerRecordingError() from exc


def _eligible_hits(
    hits: tuple[KnowledgeSearchHit, ...],
    as_of: datetime,
) -> list[KnowledgeSearchHit]:
    return [
        hit
        for hit in hits
        if hit.effective_from <= as_of
        and (hit.effective_to is None or as_of < hit.effective_to)
    ]


def _security_eligible_hits(
    hits: list[KnowledgeSearchHit],
    guard: UntrustedContentGuard,
) -> list[KnowledgeSearchHit]:
    result: list[KnowledgeSearchHit] = []
    for hit in hits:
        protected = guard.inspect_text(hit.content)
        if (
            protected.prompt_injection_detected
            or protected.sensitive_data_detected
        ):
            continue
        result.append(hit)
    return result


def _deduplicate_evidence(
    hits: list[KnowledgeSearchHit],
) -> list[_Evidence]:
    seen: set[str] = set()
    unique: list[KnowledgeSearchHit] = []
    for hit in hits:
        digest = _hash_text(hit.content)
        if digest in seen:
            continue
        seen.add(digest)
        unique.append(hit)
    return [
        _Evidence(
            evidence_id=f"K{index}",
            initial_rank=index,
            hit=hit,
        )
        for index, hit in enumerate(unique, start=1)
    ]


def _evidence_payload(evidence: list[_Evidence]) -> list[dict[str, object]]:
    return [
        {
            "evidence_id": item.evidence_id,
            "source_key": item.hit.source_key,
            "title": item.hit.title,
            "source_uri": item.hit.source_uri,
            "document_version": item.hit.document_version,
            "source_line_start": item.hit.source_line_start,
            "source_line_end": item.hit.source_line_end,
            "effective_from": item.hit.effective_from.isoformat(),
            "effective_to": (
                item.hit.effective_to.isoformat()
                if item.hit.effective_to is not None
                else None
            ),
            "content": item.hit.content,
        }
        for item in evidence
    ]


def _validate_assessment(
    output: EvidenceRerankOutput,
    evidence: list[_Evidence],
) -> dict[str, EvidenceAssessmentItem]:
    expected = {item.evidence_id for item in evidence}
    observed = [item.evidence_id for item in output.assessments]
    if len(observed) != len(set(observed)) or set(observed) != expected:
        raise EvidenceAssessmentContractError()
    conflict_keys: set[tuple[str, str]] = set()
    for conflict in output.conflicts:
        pair = tuple(
            sorted(
                (
                    conflict.left_evidence_id,
                    conflict.right_evidence_id,
                )
            )
        )
        if not set(pair) <= expected or pair in conflict_keys:
            raise EvidenceAssessmentContractError()
        conflict_keys.add(pair)
    return {item.evidence_id: item for item in output.assessments}


def _relevant_conflicts(
    conflicts: tuple[EvidenceConflictPair, ...],
    selected: list[_Evidence],
) -> set[tuple[str, str]]:
    selected_ids = {item.evidence_id for item in selected}
    return {
        tuple(
            sorted(
                (
                    conflict.left_evidence_id,
                    conflict.right_evidence_id,
                )
            )
        )
        for conflict in conflicts
        if {
            conflict.left_evidence_id,
            conflict.right_evidence_id,
        }
        <= selected_ids
    }


def _version_conflicts(
    selected: list[_Evidence],
) -> set[tuple[str, str]]:
    conflicts: set[tuple[str, str]] = set()
    for index, left in enumerate(selected):
        for right in selected[index + 1 :]:
            if (
                left.hit.source_key == right.hit.source_key
                and left.hit.document_id != right.hit.document_id
                and _hash_text(left.hit.content)
                != _hash_text(right.hit.content)
            ):
                conflicts.add(
                    tuple(sorted((left.evidence_id, right.evidence_id)))
                )
    return conflicts


def _conflict_evidence(
    selected: list[_Evidence],
    conflicts: set[tuple[str, str]],
) -> list[_Evidence]:
    conflict_ids = {item for pair in conflicts for item in pair}
    return [item for item in selected if item.evidence_id in conflict_ids]


def _verify_and_render(
    draft: GroundedAnswerDraft,
    selected: list[_Evidence],
    security_guard: UntrustedContentGuard,
) -> tuple[tuple[GroundedClaim, ...], tuple[GroundedCitation, ...]]:
    selected_by_id = {item.evidence_id: item for item in selected}
    claims: list[GroundedClaim] = []
    citations: dict[str, GroundedCitation] = {}
    quote_by_id: dict[str, str] = {}
    seen_claims: set[str] = set()
    for draft_claim in draft.claims:
        protected_claim = security_guard.inspect_text(draft_claim.text)
        if (
            protected_claim.prompt_injection_detected
            or protected_claim.sensitive_data_detected
        ):
            raise GroundingVerificationError()
        normalized_claim = _normalize(draft_claim.text)
        if (
            normalized_claim in seen_claims
            or _CITATION_MARKER.search(draft_claim.text)
        ):
            raise GroundingVerificationError()
        seen_claims.add(normalized_claim)
        citation_ids: list[str] = []
        for support in draft_claim.supports:
            evidence = selected_by_id.get(support.evidence_id)
            if (
                evidence is None
                or support.exact_quote not in evidence.hit.content
            ):
                raise GroundingVerificationError()
            previous_quote = quote_by_id.get(support.evidence_id)
            if (
                previous_quote is not None
                and previous_quote != support.exact_quote
            ):
                raise GroundingVerificationError()
            quote_by_id[support.evidence_id] = support.exact_quote
            if support.evidence_id not in citation_ids:
                citation_ids.append(support.evidence_id)
            citations[support.evidence_id] = _citation_for_evidence(
                evidence,
                support.exact_quote,
            )
        if not citation_ids:
            raise GroundingVerificationError()
        claims.append(
            GroundedClaim(
                text=draft_claim.text,
                citation_ids=tuple(citation_ids),
            )
        )
    if not claims or not citations:
        raise GroundingVerificationError()
    ordered_citations = tuple(
        citations[key]
        for key in sorted(
            citations,
            key=lambda value: int(value.removeprefix("K")),
        )
    )
    return tuple(claims), ordered_citations


def _citation_for_evidence(
    evidence: _Evidence,
    exact_quote: str,
) -> GroundedCitation:
    hit = evidence.hit
    return GroundedCitation(
        citation_id=evidence.evidence_id,
        chunk_id=hit.chunk_id,
        document_id=hit.document_id,
        source_key=hit.source_key,
        title=hit.title,
        source_uri=hit.source_uri,
        document_version=hit.document_version,
        source_line_start=hit.source_line_start,
        source_line_end=hit.source_line_end,
        exact_quote=exact_quote,
    )


def _citation_traces(
    run_id,
    claims: tuple[GroundedClaim, ...],
    citations: tuple[GroundedCitation, ...],
) -> tuple[KnowledgeAnswerCitationTrace, ...]:
    claim_indexes = {
        citation.citation_id: tuple(
            index
            for index, claim in enumerate(claims, start=1)
            if citation.citation_id in claim.citation_ids
        )
        for citation in citations
    }
    return tuple(
        KnowledgeAnswerCitationTrace(
            run_id=run_id,
            citation_id=citation.citation_id,
            chunk_id=citation.chunk_id,
            source_key=citation.source_key,
            source_uri=citation.source_uri,
            document_version=citation.document_version,
            source_line_start=citation.source_line_start,
            source_line_end=citation.source_line_end,
            quote_hash=_hash_text(citation.exact_quote),
            claim_indexes=claim_indexes[citation.citation_id],
        )
        for citation in citations
    )


def _error_code(exc: Exception) -> str:
    if isinstance(exc, KnowledgeAnsweringError):
        return exc.code
    if isinstance(exc, ModelGatewayError):
        return exc.error_code
    if isinstance(exc, KnowledgeRetrievalError):
        return exc.code
    return "knowledge_answer_unclassified_error"


def _normalize(value: str) -> str:
    return _WHITESPACE.sub(" ", value).strip().casefold()


def _hash_text(value: str) -> str:
    return sha256(_normalize(value).encode("utf-8")).hexdigest()
