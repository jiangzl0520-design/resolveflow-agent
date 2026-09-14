from datetime import UTC, datetime
from hashlib import sha256
import json
from typing import Callable, Protocol
from uuid import uuid4

from app.core.errors import StorageUnavailableError
from app.domain.knowledge import (
    IngestPolicyDocumentCommand,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIngestionResult,
    KnowledgeIngestionRun,
    KnowledgeIngestionStatus,
)
from app.knowledge.chunking import HeadingAwareChunker
from app.knowledge.errors import (
    KnowledgeIdempotencyConflictError,
    KnowledgeIngestionError,
    KnowledgeRetryBudgetExceededError,
    KnowledgeRunInProgressError,
    KnowledgeVersionConflictError,
)
from app.knowledge.markdown import MarkdownPolicyParser
from app.repositories.knowledge_repository import KnowledgeRepository


class PolicyDocumentParser(Protocol):
    def parse(self, source_text: str): ...


class PolicyDocumentChunker(Protocol):
    def chunk(self, document, *, fallback_title: str): ...


class KnowledgeIngestionService:
    def __init__(
        self,
        repository: KnowledgeRepository,
        *,
        parser: PolicyDocumentParser | None = None,
        chunker: PolicyDocumentChunker | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._parser = parser or MarkdownPolicyParser()
        self._chunker = chunker or HeadingAwareChunker()
        self._clock = clock

    def ingest(
        self,
        command: IngestPolicyDocumentCommand,
    ) -> KnowledgeIngestionResult:
        content_hash = _content_hash(command.source_text)
        request_hash = _request_hash(command, content_hash)
        now = self._clock()
        run = self._start_or_resume(
            command,
            request_hash=request_hash,
            content_hash=content_hash,
            now=now,
        )
        if run.status in {
            KnowledgeIngestionStatus.COMPLETED,
            KnowledgeIngestionStatus.SKIPPED,
        }:
            return self._result_for_terminal_run(run)

        try:
            existing = self._repository.find_document_by_source_version(
                command.tenant_id,
                command.source_key,
                command.document_version,
            )
            if existing is not None:
                if existing.content_hash != content_hash:
                    raise KnowledgeVersionConflictError()
                skipped = run.model_copy(
                    update={
                        "status": KnowledgeIngestionStatus.SKIPPED,
                        "document_id": existing.id,
                        "chunk_count": len(
                            self._repository.list_chunks(
                                command.tenant_id,
                                existing.id,
                            )
                        ),
                        "error_code": None,
                        "updated_at": now,
                        "completed_at": now,
                    }
                )
                self._repository.save_run(skipped)
                return self._result_for_terminal_run(
                    skipped,
                    deduplicated=True,
                )

            parsed = self._parser.parse(command.source_text)
            drafts = self._chunker.chunk(
                parsed,
                fallback_title=command.title,
            )
            document = KnowledgeDocument(
                id=uuid4(),
                tenant_id=command.tenant_id,
                source_key=command.source_key,
                document_version=command.document_version,
                title=command.title,
                source_uri=command.source_uri,
                media_type=command.media_type,
                content_hash=content_hash,
                raw_content=command.source_text,
                effective_from=command.effective_from,
                effective_to=command.effective_to,
                allowed_roles=command.allowed_roles,
                is_current=True,
                created_at=now,
            )
            chunks = tuple(
                KnowledgeChunk(
                    id=uuid4(),
                    tenant_id=command.tenant_id,
                    document_id=document.id,
                    chunk_index=index,
                    section_path=(
                        draft.section_path or (command.title,)
                    ),
                    content=draft.content,
                    content_hash=_text_hash(draft.content),
                    source_uri=command.source_uri,
                    document_version=command.document_version,
                    source_line_start=draft.source_line_start,
                    source_line_end=draft.source_line_end,
                    effective_from=command.effective_from,
                    effective_to=command.effective_to,
                    allowed_roles=command.allowed_roles,
                    created_at=now,
                )
                for index, draft in enumerate(drafts)
            )
            completed = run.model_copy(
                update={
                    "status": KnowledgeIngestionStatus.COMPLETED,
                    "document_id": document.id,
                    "chunk_count": len(chunks),
                    "error_code": None,
                    "updated_at": now,
                    "completed_at": now,
                }
            )
            self._repository.publish(completed, document, chunks)
            return KnowledgeIngestionResult(
                run=completed,
                document=document,
                chunks=chunks,
                deduplicated=False,
            )
        except KnowledgeIngestionError as exc:
            self._record_failure(run, exc.code, now)
            raise
        except StorageUnavailableError as exc:
            error = KnowledgeIngestionError(
                "knowledge_storage_unavailable",
                "Knowledge storage is temporarily unavailable.",
                retryable=True,
            )
            self._record_failure(run, error.code, now)
            raise error from exc
        except Exception as exc:
            error = KnowledgeIngestionError(
                "knowledge_ingestion_failed",
                "Knowledge ingestion failed without a safe public detail.",
                retryable=False,
            )
            self._record_failure(run, error.code, now)
            raise error from exc

    def _start_or_resume(
        self,
        command: IngestPolicyDocumentCommand,
        *,
        request_hash: str,
        content_hash: str,
        now: datetime,
    ) -> KnowledgeIngestionRun:
        existing = self._repository.get_run_by_idempotency_key(
            command.tenant_id,
            command.idempotency_key,
        )
        if existing is None:
            run = KnowledgeIngestionRun(
                id=uuid4(),
                tenant_id=command.tenant_id,
                idempotency_key=command.idempotency_key,
                request_hash=request_hash,
                source_key=command.source_key,
                document_version=command.document_version,
                content_hash=content_hash,
                status=KnowledgeIngestionStatus.PROCESSING,
                attempts=1,
                max_attempts=command.max_attempts,
                document_id=None,
                chunk_count=0,
                error_code=None,
                request_id=command.request_id,
                trace_id=command.trace_id,
                created_at=now,
                updated_at=now,
                completed_at=None,
            )
            self._repository.add_run(run)
            return run
        if existing.request_hash != request_hash:
            raise KnowledgeIdempotencyConflictError()
        if existing.status in {
            KnowledgeIngestionStatus.COMPLETED,
            KnowledgeIngestionStatus.SKIPPED,
        }:
            return existing
        if existing.status is KnowledgeIngestionStatus.PROCESSING:
            raise KnowledgeRunInProgressError()
        if existing.attempts >= existing.max_attempts:
            raise KnowledgeRetryBudgetExceededError()

        resumed = existing.model_copy(
            update={
                "status": KnowledgeIngestionStatus.PROCESSING,
                "attempts": existing.attempts + 1,
                "error_code": None,
                "updated_at": now,
                "completed_at": None,
            }
        )
        self._repository.save_run(resumed)
        return resumed

    def _record_failure(
        self,
        run: KnowledgeIngestionRun,
        error_code: str,
        now: datetime,
    ) -> None:
        failed = run.model_copy(
            update={
                "status": KnowledgeIngestionStatus.FAILED,
                "error_code": error_code,
                "updated_at": now,
                "completed_at": now,
            }
        )
        try:
            self._repository.save_run(failed)
        except StorageUnavailableError:
            # The caller still receives the original safe error. A total storage
            # outage can prevent recording its own failure and must be monitored.
            return

    def _result_for_terminal_run(
        self,
        run: KnowledgeIngestionRun,
        *,
        deduplicated: bool | None = None,
    ) -> KnowledgeIngestionResult:
        document = (
            self._repository.get_document(
                run.tenant_id,
                run.document_id,
            )
            if run.document_id is not None
            else None
        )
        chunks = (
            tuple(
                self._repository.list_chunks(
                    run.tenant_id,
                    run.document_id,
                )
            )
            if run.document_id is not None
            else ()
        )
        return KnowledgeIngestionResult(
            run=run,
            document=document,
            chunks=chunks,
            deduplicated=(
                run.status is KnowledgeIngestionStatus.SKIPPED
                if deduplicated is None
                else deduplicated
            ),
        )


def _content_hash(source_text: str) -> str:
    normalized = source_text.lstrip("\ufeff").replace("\r\n", "\n")
    normalized = normalized.replace("\r", "\n")
    normalized = "\n".join(
        line.rstrip() for line in normalized.split("\n")
    ).strip()
    return _text_hash(normalized)


def _request_hash(
    command: IngestPolicyDocumentCommand,
    content_hash: str,
) -> str:
    document = {
        "source_key": command.source_key,
        "document_version": command.document_version,
        "title": command.title,
        "source_uri": command.source_uri,
        "media_type": command.media_type,
        "content_hash": content_hash,
        "effective_from": command.effective_from.isoformat(),
        "effective_to": (
            command.effective_to.isoformat()
            if command.effective_to is not None
            else None
        ),
        "allowed_roles": sorted(
            role.value for role in command.allowed_roles
        ),
    }
    serialized = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return _text_hash(serialized)


def _text_hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()
