"""Add ticket versioning and idempotency records.

Revision ID: 20260724_0002
Revises: 20260724_0001
Create Date: 2026-07-24
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260724_0002"
down_revision: str | None = "20260724_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tickets",
        sa.Column(
            "version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
    )
    op.create_table(
        "idempotency_keys",
        sa.Column("operation", sa.String(length=100), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["resource_id"],
            ["tickets.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "operation",
            "key",
            name="pk_idempotency_keys",
        ),
    )
    op.create_index(
        "ix_idempotency_keys_created_at",
        "idempotency_keys",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_idempotency_keys_created_at",
        table_name="idempotency_keys",
    )
    op.drop_table("idempotency_keys")
    op.drop_column("tickets", "version")
