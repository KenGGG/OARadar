import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config


def upgrade_database(database_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).resolve().parent / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    database_path.parent.mkdir(parents=True, exist_ok=True)
    _remove_empty_alembic_temp_tables(database_path)
    command.upgrade(config, "head")


def _remove_empty_alembic_temp_tables(database_path: Path) -> None:
    """Recover known empty batch-DDL artifacts left by an interrupted migration.

    Alembic's SQLite batch mode creates these exact tables before copying rows.
    A process interruption can leave an empty table while the source table and
    Alembic revision remain unchanged.  Non-empty tables are never removed.
    """
    if not database_path.exists():
        return
    with sqlite3.connect(database_path) as connection:
        for table in (
            "_alembic_tmp_batch_items",
            "_alembic_tmp_classification_decisions",
        ):
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not exists:
                continue
            count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if count:
                raise RuntimeError(
                    f"non-empty Alembic temporary table requires manual recovery: {table}"
                )
            connection.execute(f"DROP TABLE {table}")
