"""Add pgvector hybrid retrieval, ACL metadata, and retrieval traces.

Revision ID: 20260804_0008
Revises: 20260804_0007
Create Date: 2026-08-04
"""

from collections.abc import Sequence

from alembic import op
from pgvector.sqlalchemy import VECTOR
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.knowledge.constants import KNOWLEDGE_EMBEDDING_DIMENSIONS

revision: str = "20260804_0008"
down_revision: str | None = "20260804_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_document = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
embedding_vector = sa.JSON().with_variant(
    VECTOR(KNOWLEDGE_EMBEDDING_DIMENSIONS),
    "postgresql",
)
DEFAULT_ALLOWED_ROLES = '["agent", "supervisor", "tenant_admin"]'


def upgrade() -> None:
    connection = op.get_bind()
    is_postgresql = connection.dialect.name == "postgresql"
    if is_postgresql:
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.add_column(
        "knowledge_documents",
        sa.Column(
            "allowed_roles",
            json_document,
            nullable=False,
            server_default=sa.text(f"'{DEFAULT_ALLOWED_ROLES}'"),
        ),
    )
    op.add_column(
        "knowledge_chunks",
        sa.Column(
            "allowed_roles",
            json_document,
            nullable=False,
            server_default=sa.text(f"'{DEFAULT_ALLOWED_ROLES}'"),
        ),
    )
    op.add_column(
        "knowledge_chunks",
        sa.Column("embedding", embedding_vector, nullable=True),
    )
    op.add_column(
        "knowledge_chunks",
        sa.Column("embedding_model", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "knowledge_chunks",
        sa.Column(
            "embedded_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_table(
        "knowledge_retrieval_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("actor_roles", json_document, nullable=False),
        sa.Column("query_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "rewritten_query_hash",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("rewrite_strategy", sa.String(length=64), nullable=False),
        sa.Column("search_mode", sa.String(length=30), nullable=False),
        sa.Column("embedding_model", sa.String(length=100), nullable=False),
        sa.Column("top_k", sa.Integer(), nullable=False),
        sa.Column("semantic_candidate_count", sa.Integer(), nullable=False),
        sa.Column("keyword_candidate_count", sa.Integer(), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("degraded_reason", sa.String(length=100), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_knowledge_retrieval_runs_tenant_trace",
        "knowledge_retrieval_runs",
        ["tenant_id", "trace_id", "created_at"],
    )
    op.create_table(
        "knowledge_retrieval_hits",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.Column("semantic_rank", sa.Integer(), nullable=True),
        sa.Column("keyword_rank", sa.Integer(), nullable=True),
        sa.Column("fused_score", sa.Float(), nullable=False),
        sa.Column("vector_distance", sa.Float(), nullable=True),
        sa.Column("keyword_score", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["knowledge_retrieval_runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["knowledge_chunks.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "rank"),
        sa.UniqueConstraint(
            "run_id",
            "chunk_id",
            name="uq_knowledge_retrieval_hits_run_chunk",
        ),
    )

    op.create_index(
        "ix_knowledge_documents_allowed_roles",
        "knowledge_documents",
        ["allowed_roles"],
        postgresql_using="gin" if is_postgresql else None,
    )
    op.create_index(
        "ix_knowledge_chunks_allowed_roles",
        "knowledge_chunks",
        ["allowed_roles"],
        postgresql_using="gin" if is_postgresql else None,
    )
    if is_postgresql:
        op.execute(
            "CREATE INDEX ix_knowledge_chunks_embedding_hnsw "
            "ON knowledge_chunks USING hnsw "
            "(embedding vector_cosine_ops) WHERE embedding IS NOT NULL"
        )
        op.execute(
            "CREATE INDEX ix_knowledge_chunks_content_fts "
            "ON knowledge_chunks USING gin "
            "(to_tsvector('simple', content))"
        )
        op.execute(
            "CREATE INDEX ix_knowledge_chunks_content_trgm "
            "ON knowledge_chunks USING gin (content gin_trgm_ops)"
        )


def downgrade() -> None:
    connection = op.get_bind()
    is_postgresql = connection.dialect.name == "postgresql"
    if is_postgresql:
        op.execute("DROP INDEX IF EXISTS ix_knowledge_chunks_content_trgm")
        op.execute("DROP INDEX IF EXISTS ix_knowledge_chunks_content_fts")
        op.execute("DROP INDEX IF EXISTS ix_knowledge_chunks_embedding_hnsw")
    op.drop_index(
        "ix_knowledge_chunks_allowed_roles",
        table_name="knowledge_chunks",
    )
    op.drop_index(
        "ix_knowledge_documents_allowed_roles",
        table_name="knowledge_documents",
    )
    op.drop_table("knowledge_retrieval_hits")
    op.drop_index(
        "ix_knowledge_retrieval_runs_tenant_trace",
        table_name="knowledge_retrieval_runs",
    )
    op.drop_table("knowledge_retrieval_runs")
    op.drop_column("knowledge_chunks", "embedded_at")
    op.drop_column("knowledge_chunks", "embedding_model")
    op.drop_column("knowledge_chunks", "embedding")
    op.drop_column("knowledge_chunks", "allowed_roles")
    op.drop_column("knowledge_documents", "allowed_roles")
