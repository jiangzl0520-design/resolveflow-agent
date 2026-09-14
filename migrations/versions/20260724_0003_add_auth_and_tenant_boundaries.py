"""Add authentication audit data and tenant boundaries.

Revision ID: 20260724_0003
Revises: 20260724_0002
Create Date: 2026-07-24
"""

from collections.abc import Sequence
from uuid import UUID

from alembic import op
import sqlalchemy as sa

revision: str = "20260724_0003"
down_revision: str | None = "20260724_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_TENANT_ID = UUID("00000000-0000-0000-0000-000000000000")


def upgrade() -> None:
    _add_backfilled_tenant_id("tickets")
    _add_backfilled_tenant_id("ticket_events")
    _add_backfilled_tenant_id("agent_runs")
    _add_backfilled_tenant_id("idempotency_keys")

    with op.batch_alter_table("ticket_events") as batch_op:
        batch_op.add_column(
            sa.Column(
                "actor_id",
                sa.String(length=128),
                nullable=True,
            )
        )
    ticket_events = sa.table(
        "ticket_events",
        sa.column("actor_id", sa.String(length=128)),
    )
    op.execute(
        ticket_events.update().values(actor_id="legacy-system")
    )
    with op.batch_alter_table("ticket_events") as batch_op:
        batch_op.alter_column(
            "actor_id",
            existing_type=sa.String(length=128),
            nullable=False,
        )

    with op.batch_alter_table("idempotency_keys") as batch_op:
        batch_op.drop_constraint(
            "pk_idempotency_keys",
            type_="primary",
        )
        batch_op.create_primary_key(
            "pk_idempotency_keys",
            ["tenant_id", "operation", "key"],
        )

    op.create_index(
        "ix_tickets_tenant_customer",
        "tickets",
        ["tenant_id", "customer_id"],
    )
    op.create_index(
        "ix_tickets_tenant_status",
        "tickets",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_ticket_events_tenant_ticket_created",
        "ticket_events",
        ["tenant_id", "ticket_id", "created_at"],
    )
    op.create_index(
        "ix_agent_runs_tenant_status",
        "agent_runs",
        ["tenant_id", "status"],
    )

    op.create_table(
        "authorization_audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("permission", sa.String(length=100), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.String(length=100), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=100), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_authz_audit_tenant_created",
        "authorization_audit_events",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "ix_authz_audit_tenant_resource",
        "authorization_audit_events",
        ["tenant_id", "resource_type", "resource_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_authz_audit_tenant_resource",
        table_name="authorization_audit_events",
    )
    op.drop_index(
        "ix_authz_audit_tenant_created",
        table_name="authorization_audit_events",
    )
    op.drop_table("authorization_audit_events")

    op.drop_index(
        "ix_agent_runs_tenant_status",
        table_name="agent_runs",
    )
    op.drop_index(
        "ix_ticket_events_tenant_ticket_created",
        table_name="ticket_events",
    )
    op.drop_index(
        "ix_tickets_tenant_status",
        table_name="tickets",
    )
    op.drop_index(
        "ix_tickets_tenant_customer",
        table_name="tickets",
    )

    with op.batch_alter_table("idempotency_keys") as batch_op:
        batch_op.drop_constraint(
            "pk_idempotency_keys",
            type_="primary",
        )
        batch_op.create_primary_key(
            "pk_idempotency_keys",
            ["operation", "key"],
        )

    with op.batch_alter_table("ticket_events") as batch_op:
        batch_op.drop_column("actor_id")

    _drop_tenant_id("idempotency_keys")
    _drop_tenant_id("agent_runs")
    _drop_tenant_id("ticket_events")
    _drop_tenant_id("tickets")


def _add_backfilled_tenant_id(table_name: str) -> None:
    with op.batch_alter_table(table_name) as batch_op:
        batch_op.add_column(
            sa.Column("tenant_id", sa.Uuid(), nullable=True)
        )
    table = sa.table(
        table_name,
        sa.column("tenant_id", sa.Uuid()),
    )
    op.execute(table.update().values(tenant_id=LEGACY_TENANT_ID))
    with op.batch_alter_table(table_name) as batch_op:
        batch_op.alter_column(
            "tenant_id",
            existing_type=sa.Uuid(),
            nullable=False,
        )


def _drop_tenant_id(table_name: str) -> None:
    with op.batch_alter_table(table_name) as batch_op:
        batch_op.drop_column("tenant_id")
