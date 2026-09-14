from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.observability.sqlalchemy import instrument_sqlalchemy_engine


class Database:
    """Own the application-scoped engine and its connection pool."""

    def __init__(self, database_url: str) -> None:
        engine_options: dict[str, object] = {
            "pool_pre_ping": True,
        }
        if database_url.startswith("postgresql"):
            engine_options.update(
                pool_size=5,
                max_overflow=10,
                pool_timeout=30,
            )

        self.engine: Engine = create_engine(database_url, **engine_options)
        instrument_sqlalchemy_engine(self.engine)
        self.session_factory = sessionmaker(
            bind=self.engine,
            class_=Session,
            autoflush=False,
            expire_on_commit=False,
        )

    def dispose(self) -> None:
        """Close all connections currently owned by the engine pool."""
        self.engine.dispose()
