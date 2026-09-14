"""Add grounded answer runs and citation traces.

Revision ID: 20260804_0009
Revises: 20260804_0008
Create Date: 2026-08-04
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260804_0009"
down_revision: str | None = "20260804_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_document = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "knowledge_answer_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("actor_roles", json_document, nullable=False),
        sa.Column("query_hash", sa.String(length=64), nullable=False),
        sa.Column("retrieval_run_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column(
            "retrieved_candidate_count",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "eligible_candidate_count",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "deduplicated_candidate_count",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "selected_candidate_count",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column("conflict_count", sa.Integer(), nullable=False),
        sa.Column("citation_count", sa.Integer(), nullable=False),
        sa.Column("rerank_call_id", sa.Uuid(), nullable=True),
        sa.Column("answer_call_id", sa.Uuid(), nullable=True),
        sa.Column("answer_hash", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["retrieval_run_id"],
            ["knowledge_retrieval_runs.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["rerank_call_id"],
            ["model_call_records.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["answer_call_id"],
            ["model_call_records.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_knowledge_answer_runs_tenant_trace",
        "knowledge_answer_runs",
        ["tenant_id", "trace_id", "created_at"],
    )
    op.create_index(
        "ix_knowledge_answer_runs_tenant_status",
        "knowledge_answer_runs",
        ["tenant_id", "status", "created_at"],
    )
    op.create_table(
        "knowledge_answer_citations",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("citation_id", sa.String(length=10), nullable=False),
        sa.Column("chunk_id", sa.Uuid(), nullable=True),
        sa.Column("source_key", sa.String(length=128), nullable=False),
        sa.Column("source_uri", sa.String(length=500), nullable=False),
        sa.Column(
            "document_version",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("source_line_start", sa.Integer(), nullable=False),
        sa.Column("source_line_end", sa.Integer(), nullable=False),
        sa.Column("quote_hash", sa.String(length=64), nullable=False),
        sa.Column("claim_indexes", json_document, nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["knowledge_answer_runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["knowledge_chunks.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("run_id", "citation_id"),
    )


def downgrade() -> None:
    op.drop_table("knowledge_answer_citations")
    op.drop_index(
        "ix_knowledge_answer_runs_tenant_status",
        table_name="knowledge_answer_runs",
    )
    op.drop_index(
        "ix_knowledge_answer_runs_tenant_trace",
        table_name="knowledge_answer_runs",
    )
    op.drop_table("knowledge_answer_runs")
