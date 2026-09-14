from pathlib import Path

from alembic import command
from sqlalchemy import create_engine, inspect

from tests.integration.database_helpers import (
    database_config,
    downgrade_database,
    migrated_sqlite_url,
)

CORE_TABLES = {
    "tickets",
    "ticket_events",
    "agent_runs",
    "idempotency_keys",
    "authorization_audit_events",
    "investigation_jobs",
    "investigation_job_events",
    "model_call_records",
    "context_build_runs",
    "context_fragment_traces",
    "long_term_memories",
    "memory_audit_events",
}


def test_upgrade_creates_core_tables_and_foreign_keys(tmp_path: Path) -> None:
    database_url = migrated_sqlite_url(tmp_path)
    engine = create_engine(database_url)
    inspector = inspect(engine)

    assert CORE_TABLES <= set(inspector.get_table_names())
    assert inspector.get_pk_constraint("tickets")["constrained_columns"] == ["id"]
    assert {
        foreign_key["referred_table"]
        for foreign_key in inspector.get_foreign_keys("ticket_events")
    } == {"tickets"}
    assert inspector.get_pk_constraint("idempotency_keys")[
        "constrained_columns"
    ] == ["tenant_id", "operation", "key"]
    assert any(
        column["name"] == "version" and not column["nullable"]
        for column in inspector.get_columns("tickets")
    )
    assert {
        foreign_key["referred_table"]
        for foreign_key in inspector.get_foreign_keys("agent_runs")
    } == {"tickets"}
    assert {
        foreign_key["referred_table"]
        for foreign_key in inspector.get_foreign_keys("investigation_jobs")
    } == {"tickets"}
    assert {
        foreign_key["referred_table"]
        for foreign_key in inspector.get_foreign_keys(
            "investigation_job_events"
        )
    } == {"investigation_jobs"}
    for table_name in (
        "tickets",
        "ticket_events",
        "agent_runs",
        "idempotency_keys",
        "investigation_jobs",
        "investigation_job_events",
        "model_call_records",
        "context_build_runs",
        "long_term_memories",
        "memory_audit_events",
    ):
        assert any(
            column["name"] == "tenant_id" and not column["nullable"]
            for column in inspector.get_columns(table_name)
        )
    assert any(
        column["name"] == "actor_id" and not column["nullable"]
        for column in inspector.get_columns("ticket_events")
    )
    investigation_job_columns = {
        column["name"]: column
        for column in inspector.get_columns("investigation_jobs")
    }
    for column_name in (
        "status",
        "attempts",
        "max_attempts",
        "version",
        "lease_token",
        "lease_expires_at",
        "next_attempt_at",
        "cancel_requested_at",
        "dispatched_at",
        "trace_id",
    ):
        assert column_name in investigation_job_columns
    assert not investigation_job_columns["version"]["nullable"]
    unique_constraints = {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints(
            "investigation_jobs"
        )
    }
    assert (
        "tenant_id",
        "job_type",
        "idempotency_key",
    ) in unique_constraints
    model_call_columns = {
        column["name"]: column
        for column in inspector.get_columns("model_call_records")
    }
    for column_name in (
        "provider",
        "model",
        "prompt_name",
        "prompt_version",
        "prompt_hash",
        "response_schema_name",
        "response_schema_hash",
        "resource_type",
        "resource_id",
        "status",
        "attempts",
        "attempt_error_codes",
        "latency_ms",
        "request_id",
        "trace_id",
        "context_build_id",
    ):
        assert column_name in model_call_columns

    engine.dispose()


def test_downgrade_removes_core_tables(tmp_path: Path) -> None:
    database_url = migrated_sqlite_url(tmp_path)

    downgrade_database(database_url)

    engine = create_engine(database_url)
    assert CORE_TABLES.isdisjoint(inspect(engine).get_table_names())
    engine.dispose()


def test_migration_is_in_sync_with_orm_metadata(tmp_path: Path) -> None:
    database_url = migrated_sqlite_url(tmp_path)

    command.check(database_config(database_url))
