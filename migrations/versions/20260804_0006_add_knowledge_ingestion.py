"""Add traceable knowledge documents, chunks, and ingestion runs.

Revision ID: 20260804_0007
Revises: 20260724_0006
Create Date: 2026-08-04
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260804_0007"
down_revision: str | None = "20260724_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_document = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("source_key", sa.String(length=128), nullable=False),
        sa.Column(
            "document_version",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("source_uri", sa.String(length=500), nullable=False),
        sa.Column("media_type", sa.String(length=50), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("raw_content", sa.Text(), nullable=False),
        sa.Column(
            "effective_from",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "effective_to",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "source_key",
            "document_version",
            name="uq_knowledge_documents_tenant_source_version",
        ),
    )
    op.create_index(
        "ix_knowledge_documents_tenant_source_current",
        "knowledge_documents",
        ["tenant_id", "source_key", "is_current"],
    )
    op.create_index(
        "ix_knowledge_documents_tenant_effective",
        "knowledge_documents",
        ["tenant_id", "effective_from", "effective_to"],
    )

    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("section_path", json_document, nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("source_uri", sa.String(length=500), nullable=False),
        sa.Column(
            "document_version",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("source_line_start", sa.Integer(), nullable=False),
        sa.Column("source_line_end", sa.Integer(), nullable=False),
        sa.Column(
            "effective_from",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "effective_to",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["knowledge_documents.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_id",
            "chunk_index",
            name="uq_knowledge_chunks_document_index",
        ),
    )
    op.create_index(
        "ix_knowledge_chunks_tenant_document",
        "knowledge_chunks",
        ["tenant_id", "document_id", "chunk_index"],
    )
    op.create_index(
        "ix_knowledge_chunks_tenant_effective",
        "knowledge_chunks",
        ["tenant_id", "effective_from", "effective_to"],
    )
    op.create_index(
        "ix_knowledge_chunks_tenant_content_hash",
        "knowledge_chunks",
        ["tenant_id", "content_hash"],
    )

    op.create_table(
        "knowledge_ingestion_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("source_key", sa.String(length=128), nullable=False),
        sa.Column(
            "document_version",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["knowledge_documents.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_knowledge_ingestion_tenant_key",
        ),
    )
    op.create_index(
        "ix_knowledge_ingestion_tenant_status",
        "knowledge_ingestion_runs",
        ["tenant_id", "status", "updated_at"],
    )
    op.create_index(
        "ix_knowledge_ingestion_tenant_source_version",
        "knowledge_ingestion_runs",
        ["tenant_id", "source_key", "document_version"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_knowledge_ingestion_tenant_source_version",
        table_name="knowledge_ingestion_runs",
    )
    op.drop_index(
        "ix_knowledge_ingestion_tenant_status",
        table_name="knowledge_ingestion_runs",
    )
    op.drop_table("knowledge_ingestion_runs")

    op.drop_index(
        "ix_knowledge_chunks_tenant_content_hash",
        table_name="knowledge_chunks",
    )
    op.drop_index(
        "ix_knowledge_chunks_tenant_effective",
        table_name="knowledge_chunks",
    )
    op.drop_index(
        "ix_knowledge_chunks_tenant_document",
        table_name="knowledge_chunks",
    )
    op.drop_table("knowledge_chunks")

    op.drop_index(
        "ix_knowledge_documents_tenant_effective",
        table_name="knowledge_documents",
    )
    op.drop_index(
        "ix_knowledge_documents_tenant_source_current",
        table_name="knowledge_documents",
    )
    op.drop_table("knowledge_documents")
