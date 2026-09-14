from collections.abc import Iterator
from contextlib import contextmanager

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg import Connection
from psycopg.rows import dict_row


LANGGRAPH_MANAGED_TABLES = frozenset(
    {
        "checkpoint_migrations",
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
    }
)


def psycopg_connection_string(database_url: str) -> str:
    """Convert a SQLAlchemy PostgreSQL URL into a Psycopg connection URL."""
    if database_url.startswith("postgresql+psycopg://"):
        return database_url.replace(
            "postgresql+psycopg://",
            "postgresql://",
            1,
        )
    if database_url.startswith("postgresql://"):
        return database_url
    raise ValueError("LangGraph checkpoints require PostgreSQL.")


def safe_checkpoint_serializer() -> JsonPlusSerializer:
    """Allow built-in checkpoint values but no project-class imports."""
    return JsonPlusSerializer(allowed_msgpack_modules=())


@contextmanager
def postgres_checkpointer(
    database_url: str,
    *,
    setup: bool = False,
) -> Iterator[PostgresSaver]:
    """Own one synchronous PostgreSQL checkpointer connection."""
    connection_string = psycopg_connection_string(database_url)
    with Connection.connect(
        connection_string,
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
    ) as connection:
        saver = PostgresSaver(
            connection,
            serde=safe_checkpoint_serializer(),
        )
        if setup:
            saver.setup()
        yield saver
