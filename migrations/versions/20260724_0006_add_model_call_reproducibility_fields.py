"""Add model call schema hash and resource linkage.

Revision ID: 20260724_0006
Revises: 20260724_0005
Create Date: 2026-07-24
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260724_0006"
down_revision: str | None = "20260724_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("model_call_records") as batch_op:
        batch_op.add_column(
            sa.Column(
                "response_schema_hash",
                sa.String(length=64),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "resource_type",
                sa.String(length=100),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "resource_id",
                sa.String(length=128),
                nullable=True,
            )
        )

    model_calls = sa.table(
        "model_call_records",
        sa.column("response_schema_hash", sa.String(length=64)),
        sa.column("resource_type", sa.String(length=100)),
        sa.column("resource_id", sa.String(length=128)),
    )
    op.execute(
        model_calls.update().values(
            response_schema_hash="legacy-unavailable",
            resource_type="unknown",
            resource_id="legacy-unavailable",
        )
    )

    with op.batch_alter_table("model_call_records") as batch_op:
        batch_op.alter_column(
            "response_schema_hash",
            existing_type=sa.String(length=64),
            nullable=False,
        )
        batch_op.alter_column(
            "resource_type",
            existing_type=sa.String(length=100),
            nullable=False,
        )
        batch_op.alter_column(
            "resource_id",
            existing_type=sa.String(length=128),
            nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("model_call_records") as batch_op:
        batch_op.drop_column("resource_id")
        batch_op.drop_column("resource_type")
        batch_op.drop_column("response_schema_hash")
