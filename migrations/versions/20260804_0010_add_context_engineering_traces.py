"""Add context-build decisions and model-call linkage.

Revision ID: 20260804_0010
Revises: 20260804_0009
Create Date: 2026-08-04
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260804_0010"
down_revision: str | None = "20260804_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "context_build_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=False),
        sa.Column("step_number", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("context_window_tokens", sa.Integer(), nullable=False),
        sa.Column("reserved_output_tokens", sa.Integer(), nullable=False),
        sa.Column("reserved_reasoning_tokens", sa.Integer(), nullable=False),
        sa.Column("safety_margin_tokens", sa.Integer(), nullable=False),
        sa.Column("input_budget_tokens", sa.Integer(), nullable=False),
        sa.Column("actual_input_tokens", sa.Integer(), nullable=False),
        sa.Column("included_fragment_count", sa.Integer(), nullable=False),
        sa.Column("dropped_fragment_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_context_runs_tenant_trace",
        "context_build_runs",
        ["tenant_id", "trace_id", "created_at"],
    )
    op.create_index(
        "ix_context_runs_agent_step",
        "context_build_runs",
        ["agent_run_id", "step_number"],
    )
    op.create_table(
        "context_fragment_traces",
        sa.Column("build_run_id", sa.Uuid(), nullable=False),
        sa.Column("fragment_id", sa.String(length=128), nullable=False),
        sa.Column("semantic_key", sa.String(length=128), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("trust_level", sa.String(length=50), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("relevance", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("estimated_tokens", sa.Integer(), nullable=False),
        sa.Column("included", sa.Boolean(), nullable=False),
        sa.Column("decision_reason", sa.String(length=50), nullable=False),
        sa.ForeignKeyConstraint(
            ["build_run_id"],
            ["context_build_runs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("build_run_id", "fragment_id"),
    )
    op.create_index(
        "ix_context_fragments_source_decision",
        "context_fragment_traces",
        ["source", "decision_reason"],
    )
    with op.batch_alter_table("model_call_records") as batch_op:
        batch_op.add_column(
            sa.Column("context_build_id", sa.Uuid(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_model_calls_context_build_id",
            "context_build_runs",
            ["context_build_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("model_call_records") as batch_op:
        batch_op.drop_constraint(
            "fk_model_calls_context_build_id",
            type_="foreignkey",
        )
        batch_op.drop_column("context_build_id")
    op.drop_index(
        "ix_context_fragments_source_decision",
        table_name="context_fragment_traces",
    )
    op.drop_table("context_fragment_traces")
    op.drop_index(
        "ix_context_runs_agent_step",
        table_name="context_build_runs",
    )
    op.drop_index(
        "ix_context_runs_tenant_trace",
        table_name="context_build_runs",
    )
    op.drop_table("context_build_runs")
