from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from pgvector.sqlalchemy import VECTOR

from app.db.base import Base
from app.knowledge.constants import KNOWLEDGE_EMBEDDING_DIMENSIONS

JsonDocument = JSON().with_variant(JSONB(), "postgresql")
EmbeddingVector = JSON().with_variant(
    VECTOR(KNOWLEDGE_EMBEDDING_DIMENSIONS),
    "postgresql",
)


class TicketRecord(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        Index("ix_tickets_customer_id", "customer_id"),
        Index("ix_tickets_status", "status"),
        Index("ix_tickets_tenant_customer", "tenant_id", "customer_id"),
        Index("ix_tickets_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )


class IdempotencyRecordModel(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id",
            "operation",
            "key",
            name="pk_idempotency_keys",
        ),
        Index("ix_idempotency_keys_created_at", "created_at"),
    )

    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    operation: Mapped[str] = mapped_column(String(100), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tickets.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class TicketEventRecord(Base):
    __tablename__ = "ticket_events"
    __table_args__ = (
        Index("ix_ticket_events_ticket_id_created_at", "ticket_id", "created_at"),
        Index(
            "ix_ticket_events_tenant_ticket_created",
            "tenant_id",
            "ticket_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    ticket_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tickets.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(50), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JsonDocument, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class AgentRunRecord(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        Index("ix_agent_runs_ticket_id", "ticket_id"),
        Index("ix_agent_runs_status", "status"),
        Index("ix_agent_runs_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    ticket_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tickets.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    current_node: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[dict[str, Any]] = mapped_column(
        JsonDocument, nullable=False, default=dict
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


class AuthorizationAuditRecord(Base):
    __tablename__ = "authorization_audit_events"
    __table_args__ = (
        Index(
            "ix_authz_audit_tenant_created",
            "tenant_id",
            "created_at",
        ),
        Index(
            "ix_authz_audit_tenant_resource",
            "tenant_id",
            "resource_type",
            "resource_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    permission: Mapped[str] = mapped_column(String(100), nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(String(100), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class InvestigationJobRecord(Base):
    __tablename__ = "investigation_jobs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "job_type",
            "idempotency_key",
            name="uq_investigation_jobs_tenant_type_key",
        ),
        Index(
            "ix_investigation_jobs_tenant_ticket",
            "tenant_id",
            "ticket_id",
        ),
        Index(
            "ix_investigation_jobs_dispatch",
            "dispatched_at",
            "status",
            "created_at",
        ),
        Index(
            "ix_investigation_jobs_recovery",
            "status",
            "next_attempt_at",
            "lease_expires_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    ticket_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tickets.id", ondelete="CASCADE"),
        nullable=False,
    )
    job_type: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_roles: Mapped[list[str]] = mapped_column(
        JsonDocument, nullable=False, default=list
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    request_hash: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    lease_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    dispatch_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_dispatch_error_code: Mapped[str | None] = mapped_column(
        String(100)
    )
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


class InvestigationJobEventRecord(Base):
    __tablename__ = "investigation_job_events"
    __table_args__ = (
        Index(
            "ix_investigation_job_events_job_created",
            "job_id",
            "created_at",
        ),
        Index(
            "ix_investigation_job_events_tenant_created",
            "tenant_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    job_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("investigation_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(50))
    to_status: Mapped[str] = mapped_column(String(50), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(100), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JsonDocument, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ModelCallRecordModel(Base):
    __tablename__ = "model_call_records"
    __table_args__ = (
        Index(
            "ix_model_calls_tenant_trace",
            "tenant_id",
            "trace_id",
            "started_at",
        ),
        Index(
            "ix_model_calls_tenant_status",
            "tenant_id",
            "status",
            "started_at",
        ),
        Index(
            "ix_model_calls_prompt_version",
            "prompt_name",
            "prompt_version",
            "started_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    context_build_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("context_build_runs.id", ondelete="SET NULL"),
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    operation: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_name: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(
        String(50), nullable=False
    )
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_schema_name: Mapped[str] = mapped_column(
        String(100), nullable=False
    )
    response_schema_hash: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    resource_type: Mapped[str] = mapped_column(
        String(100), nullable=False
    )
    resource_id: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_error_codes: Mapped[list[str]] = mapped_column(
        JsonDocument, nullable=False, default=list
    )
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    provider_request_id: Mapped[str | None] = mapped_column(String(128))
    provider_response_id: Mapped[str | None] = mapped_column(String(128))
    error_code: Mapped[str | None] = mapped_column(String(100))
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ContextBuildRunRecordModel(Base):
    __tablename__ = "context_build_runs"
    __table_args__ = (
        Index(
            "ix_context_runs_tenant_trace",
            "tenant_id",
            "trace_id",
            "created_at",
        ),
        Index(
            "ix_context_runs_agent_step",
            "agent_run_id",
            "step_number",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    agent_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    step_number: Mapped[int] = mapped_column(Integer, nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    context_window_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    reserved_output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    reserved_reasoning_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    safety_margin_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    input_budget_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    actual_input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    included_fragment_count: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    dropped_fragment_count: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    error_code: Mapped[str | None] = mapped_column(String(100))
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ContextFragmentTraceRecordModel(Base):
    __tablename__ = "context_fragment_traces"
    __table_args__ = (
        PrimaryKeyConstraint("build_run_id", "fragment_id"),
        Index(
            "ix_context_fragments_source_decision",
            "source",
            "decision_reason",
        ),
    )

    build_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("context_build_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    fragment_id: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    semantic_key: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    trust_level: Mapped[str] = mapped_column(String(50), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    relevance: Mapped[int] = mapped_column(Integer, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    included: Mapped[bool] = mapped_column(Boolean, nullable=False)
    decision_reason: Mapped[str] = mapped_column(
        String(50), nullable=False
    )


class LongTermMemoryRecord(Base):
    __tablename__ = "long_term_memories"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "subject_id",
            "memory_key",
            "version",
            name="uq_memory_subject_key_version",
        ),
        Index(
            "ix_memories_tenant_subject_status",
            "tenant_id",
            "subject_id",
            "status",
            "expires_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    memory_key: Mapped[str] = mapped_column(String(100), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    value: Mapped[str | None] = mapped_column(String(500))
    value_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_reference_hash: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_by_actor_id: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class MemoryAuditEventRecord(Base):
    __tablename__ = "memory_audit_events"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_memory_event_tenant_idempotency",
        ),
        Index(
            "ix_memory_events_tenant_trace",
            "tenant_id",
            "trace_id",
            "created_at",
        ),
        Index(
            "ix_memory_events_subject_key",
            "tenant_id",
            "subject_id",
            "memory_key",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    memory_key: Mapped[str] = mapped_column(String(100), nullable=False)
    memory_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("long_term_memories.id", ondelete="SET NULL"),
    )
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    decision: Mapped[str] = mapped_column(String(50), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class KnowledgeDocumentRecord(Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "source_key",
            "document_version",
            name="uq_knowledge_documents_tenant_source_version",
        ),
        Index(
            "ix_knowledge_documents_tenant_source_current",
            "tenant_id",
            "source_key",
            "is_current",
        ),
        Index(
            "ix_knowledge_documents_tenant_effective",
            "tenant_id",
            "effective_from",
            "effective_to",
        ),
        Index(
            "ix_knowledge_documents_allowed_roles",
            "allowed_roles",
            postgresql_using="gin",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    source_key: Mapped[str] = mapped_column(String(128), nullable=False)
    document_version: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    source_uri: Mapped[str] = mapped_column(String(500), nullable=False)
    media_type: Mapped[str] = mapped_column(String(50), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_content: Mapped[str] = mapped_column(Text, nullable=False)
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    effective_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    allowed_roles: Mapped[list[str]] = mapped_column(
        JsonDocument,
        nullable=False,
    )
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class KnowledgeChunkRecord(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "chunk_index",
            name="uq_knowledge_chunks_document_index",
        ),
        Index(
            "ix_knowledge_chunks_tenant_document",
            "tenant_id",
            "document_id",
            "chunk_index",
        ),
        Index(
            "ix_knowledge_chunks_tenant_effective",
            "tenant_id",
            "effective_from",
            "effective_to",
        ),
        Index(
            "ix_knowledge_chunks_tenant_content_hash",
            "tenant_id",
            "content_hash",
        ),
        Index(
            "ix_knowledge_chunks_allowed_roles",
            "allowed_roles",
            postgresql_using="gin",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    section_path: Mapped[list[str]] = mapped_column(
        JsonDocument, nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_uri: Mapped[str] = mapped_column(String(500), nullable=False)
    document_version: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    source_line_start: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    source_line_end: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    effective_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    allowed_roles: Mapped[list[str]] = mapped_column(
        JsonDocument,
        nullable=False,
    )
    embedding: Mapped[list[float] | None] = mapped_column(EmbeddingVector)
    embedding_model: Mapped[str | None] = mapped_column(String(100))
    embedded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class KnowledgeRetrievalRunRecord(Base):
    __tablename__ = "knowledge_retrieval_runs"
    __table_args__ = (
        Index(
            "ix_knowledge_retrieval_runs_tenant_trace",
            "tenant_id",
            "trace_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_roles: Mapped[list[str]] = mapped_column(
        JsonDocument, nullable=False
    )
    query_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    rewritten_query_hash: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    rewrite_strategy: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    search_mode: Mapped[str] = mapped_column(String(30), nullable=False)
    embedding_model: Mapped[str] = mapped_column(
        String(100), nullable=False
    )
    top_k: Mapped[int] = mapped_column(Integer, nullable=False)
    semantic_candidate_count: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    keyword_candidate_count: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    result_count: Mapped[int] = mapped_column(Integer, nullable=False)
    degraded_reason: Mapped[str | None] = mapped_column(String(100))
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class KnowledgeRetrievalHitRecord(Base):
    __tablename__ = "knowledge_retrieval_hits"
    __table_args__ = (
        PrimaryKeyConstraint("run_id", "rank"),
        UniqueConstraint(
            "run_id",
            "chunk_id",
            name="uq_knowledge_retrieval_hits_run_chunk",
        ),
    )

    run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("knowledge_retrieval_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("knowledge_chunks.id", ondelete="CASCADE"),
        nullable=False,
    )
    semantic_rank: Mapped[int | None] = mapped_column(Integer)
    keyword_rank: Mapped[int | None] = mapped_column(Integer)
    fused_score: Mapped[float] = mapped_column(Float, nullable=False)
    vector_distance: Mapped[float | None] = mapped_column(Float)
    keyword_score: Mapped[float | None] = mapped_column(Float)


class KnowledgeAnswerRunRecord(Base):
    __tablename__ = "knowledge_answer_runs"
    __table_args__ = (
        Index(
            "ix_knowledge_answer_runs_tenant_trace",
            "tenant_id",
            "trace_id",
            "created_at",
        ),
        Index(
            "ix_knowledge_answer_runs_tenant_status",
            "tenant_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_roles: Mapped[list[str]] = mapped_column(
        JsonDocument, nullable=False
    )
    query_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    retrieval_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("knowledge_retrieval_runs.id", ondelete="SET NULL"),
    )
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    retrieved_candidate_count: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    eligible_candidate_count: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    deduplicated_candidate_count: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    selected_candidate_count: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    conflict_count: Mapped[int] = mapped_column(Integer, nullable=False)
    citation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    rerank_call_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("model_call_records.id", ondelete="SET NULL"),
    )
    answer_call_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("model_call_records.id", ondelete="SET NULL"),
    )
    answer_hash: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(100))
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


class KnowledgeAnswerCitationRecord(Base):
    __tablename__ = "knowledge_answer_citations"
    __table_args__ = (
        PrimaryKeyConstraint("run_id", "citation_id"),
    )

    run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("knowledge_answer_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    citation_id: Mapped[str] = mapped_column(String(10), nullable=False)
    chunk_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("knowledge_chunks.id", ondelete="SET NULL"),
    )
    source_key: Mapped[str] = mapped_column(String(128), nullable=False)
    source_uri: Mapped[str] = mapped_column(String(500), nullable=False)
    document_version: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    source_line_start: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    source_line_end: Mapped[int] = mapped_column(Integer, nullable=False)
    quote_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    claim_indexes: Mapped[list[int]] = mapped_column(
        JsonDocument, nullable=False
    )


class KnowledgeIngestionRunRecord(Base):
    __tablename__ = "knowledge_ingestion_runs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_knowledge_ingestion_tenant_key",
        ),
        Index(
            "ix_knowledge_ingestion_tenant_status",
            "tenant_id",
            "status",
            "updated_at",
        ),
        Index(
            "ix_knowledge_ingestion_tenant_source_version",
            "tenant_id",
            "source_key",
            "document_version",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_key: Mapped[str] = mapped_column(String(128), nullable=False)
    document_version: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    document_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("knowledge_documents.id", ondelete="SET NULL"),
    )
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
