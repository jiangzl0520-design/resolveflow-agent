from pathlib import Path

from alembic import command
from alembic.config import Config


def database_config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    return config


def migrated_sqlite_url(tmp_path: Path) -> str:
    database_file = tmp_path / "resolveflow-test.sqlite3"
    database_url = f"sqlite+pysqlite:///{database_file.as_posix()}"
    upgrade_database(database_url)
    return database_url


def upgrade_database(database_url: str) -> None:
    command.upgrade(database_config(database_url), "head")


def downgrade_database(database_url: str) -> None:
    command.downgrade(database_config(database_url), "base")
