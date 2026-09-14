"""Add policy-controlled long-term memories and audit events.

Revision ID: 20260805_0011
Revises: 20260804_0010
Create Date: 2026-08-05
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260805_0011"
down_revision: str | None = "20260804_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "long_term_memories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("memory_key", sa.String(length=100), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("value", sa.String(length=500), nullable=True),
        sa.Column("value_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_type", sa.String(length=50), nullable=False),
        sa.Column(
            "source_reference_hash",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_by_actor_id",
            sa.String(length=128),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "subject_id",
            "memory_key",
            "version",
            name="uq_memory_subject_key_version",
        ),
    )
    op.create_index(
        "ix_memories_tenant_subject_status",
        "long_term_memories",
        ["tenant_id", "subject_id", "status", "expires_at"],
    )
    op.create_table(
        "memory_audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("memory_key", sa.String(length=100), nullable=False),
        sa.Column("memory_id", sa.Uuid(), nullable=True),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("decision", sa.String(length=50), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("candidate_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["memory_id"],
            ["long_term_memories.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_memory_event_tenant_idempotency",
        ),
    )
    op.create_index(
        "ix_memory_events_tenant_trace",
        "memory_audit_events",
        ["tenant_id", "trace_id", "created_at"],
    )
    op.create_index(
        "ix_memory_events_subject_key",
        "memory_audit_events",
        ["tenant_id", "subject_id", "memory_key", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_memory_events_subject_key",
        table_name="memory_audit_events",
    )
    op.drop_index(
        "ix_memory_events_tenant_trace",
        table_name="memory_audit_events",
    )
    op.drop_table("memory_audit_events")
    op.drop_index(
        "ix_memories_tenant_subject_status",
        table_name="long_term_memories",
    )
    op.drop_table("long_term_memories")
