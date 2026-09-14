"""Add provider-neutral model call records.

Revision ID: 20260724_0005
Revises: 20260724_0004
Create Date: 2026-07-24
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260724_0005"
down_revision: str | None = "20260724_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_document = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "model_call_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.String(length=100), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("prompt_name", sa.String(length=100), nullable=False),
        sa.Column("prompt_version", sa.String(length=50), nullable=False),
        sa.Column("prompt_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "response_schema_name",
            sa.String(length=100),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("attempt_error_codes", json_document, nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column(
            "provider_request_id",
            sa.String(length=128),
            nullable=True,
        ),
        sa.Column(
            "provider_response_id",
            sa.String(length=128),
            nullable=True,
        ),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_model_calls_tenant_trace",
        "model_call_records",
        ["tenant_id", "trace_id", "started_at"],
    )
    op.create_index(
        "ix_model_calls_tenant_status",
        "model_call_records",
        ["tenant_id", "status", "started_at"],
    )
    op.create_index(
        "ix_model_calls_prompt_version",
        "model_call_records",
        ["prompt_name", "prompt_version", "started_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_model_calls_prompt_version",
        table_name="model_call_records",
    )
    op.drop_index(
        "ix_model_calls_tenant_status",
        table_name="model_call_records",
    )
    op.drop_index(
        "ix_model_calls_tenant_trace",
        table_name="model_call_records",
    )
    op.drop_table("model_call_records")
