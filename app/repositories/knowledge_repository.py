from threading import RLock
from typing import Protocol
from uuid import UUID

from app.core.errors import StorageUnavailableError
from app.domain.knowledge import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIngestionRun,
)


class KnowledgeRepository(Protocol):
    def get_run_by_idempotency_key(
        self,
        tenant_id: UUID,
        idempotency_key: str,
    ) -> KnowledgeIngestionRun | None: ...

    def add_run(self, run: KnowledgeIngestionRun) -> None: ...

    def save_run(self, run: KnowledgeIngestionRun) -> None: ...

    def find_document_by_source_version(
        self,
        tenant_id: UUID,
        source_key: str,
        document_version: str,
    ) -> KnowledgeDocument | None: ...

    def get_document(
        self,
        tenant_id: UUID,
        document_id: UUID,
    ) -> KnowledgeDocument | None: ...

    def list_chunks(
        self,
        tenant_id: UUID,
        document_id: UUID,
    ) -> list[KnowledgeChunk]: ...

    def publish(
        self,
        run: KnowledgeIngestionRun,
        document: KnowledgeDocument,
        chunks: tuple[KnowledgeChunk, ...],
    ) -> None: ...


class InMemoryKnowledgeRepository:
    def __init__(self) -> None:
        self._runs: dict[UUID, KnowledgeIngestionRun] = {}
        self._run_keys: dict[tuple[UUID, str], UUID] = {}
        self._documents: dict[UUID, KnowledgeDocument] = {}
        self._document_versions: dict[tuple[UUID, str, str], UUID] = {}
        self._chunks: dict[UUID, list[KnowledgeChunk]] = {}
        self._publish_failures = 0
        self._lock = RLock()

    def fail_next_publish(self, *, count: int = 1) -> None:
        with self._lock:
            self._publish_failures = count

    def get_run_by_idempotency_key(
        self,
        tenant_id: UUID,
        idempotency_key: str,
    ) -> KnowledgeIngestionRun | None:
        with self._lock:
            run_id = self._run_keys.get((tenant_id, idempotency_key))
            return self._runs.get(run_id) if run_id is not None else None

    def add_run(self, run: KnowledgeIngestionRun) -> None:
        with self._lock:
            key = (run.tenant_id, run.idempotency_key)
            if key in self._run_keys:
                raise StorageUnavailableError(
                    "Knowledge ingestion run already exists."
                )
            self._runs[run.id] = run
            self._run_keys[key] = run.id

    def save_run(self, run: KnowledgeIngestionRun) -> None:
        with self._lock:
            if run.id not in self._runs:
                raise StorageUnavailableError(
                    "Knowledge ingestion run does not exist."
                )
            self._runs[run.id] = run

    def find_document_by_source_version(
        self,
        tenant_id: UUID,
        source_key: str,
        document_version: str,
    ) -> KnowledgeDocument | None:
        with self._lock:
            document_id = self._document_versions.get(
                (tenant_id, source_key, document_version)
            )
            return (
                self._documents.get(document_id)
                if document_id is not None
                else None
            )

    def get_document(
        self,
        tenant_id: UUID,
        document_id: UUID,
    ) -> KnowledgeDocument | None:
        with self._lock:
            document = self._documents.get(document_id)
            if document is None or document.tenant_id != tenant_id:
                return None
            return document

    def list_chunks(
        self,
        tenant_id: UUID,
        document_id: UUID,
    ) -> list[KnowledgeChunk]:
        with self._lock:
            return [
                chunk
                for chunk in self._chunks.get(document_id, [])
                if chunk.tenant_id == tenant_id
            ]

    def publish(
        self,
        run: KnowledgeIngestionRun,
        document: KnowledgeDocument,
        chunks: tuple[KnowledgeChunk, ...],
    ) -> None:
        with self._lock:
            if self._publish_failures:
                self._publish_failures -= 1
                raise StorageUnavailableError(
                    "Synthetic knowledge publish failure."
                )
            version_key = (
                document.tenant_id,
                document.source_key,
                document.document_version,
            )
            if version_key in self._document_versions:
                raise StorageUnavailableError(
                    "Knowledge source version already exists."
                )
            for document_id, current in list(self._documents.items()):
                if (
                    current.tenant_id == document.tenant_id
                    and current.source_key == document.source_key
                    and current.is_current
                ):
                    self._documents[document_id] = current.model_copy(
                        update={"is_current": False}
                    )
            self._documents[document.id] = document
            self._document_versions[version_key] = document.id
            self._chunks[document.id] = list(chunks)
            self._runs[run.id] = run

    @property
    def runs(self) -> list[KnowledgeIngestionRun]:
        with self._lock:
            return list(self._runs.values())

    @property
    def documents(self) -> list[KnowledgeDocument]:
        with self._lock:
            return list(self._documents.values())

    @property
    def chunks(self) -> list[KnowledgeChunk]:
        with self._lock:
            return [
                chunk
                for chunks in self._chunks.values()
                for chunk in chunks
            ]

