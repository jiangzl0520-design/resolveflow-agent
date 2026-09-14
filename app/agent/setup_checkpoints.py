from app.agent.checkpointing import postgres_checkpointer
from app.core.config import Settings


def main() -> None:
    settings = Settings.from_env()
    with postgres_checkpointer(
        settings.database_url,
        setup=True,
    ):
        pass
    print("LangGraph checkpoint tables are ready.")


if __name__ == "__main__":
    main()
