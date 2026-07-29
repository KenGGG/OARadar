from pathlib import Path
import sqlite3
from alembic import command
from alembic.config import Config


def upgrade_database(database_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).resolve().parent / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    database_path.parent.mkdir(parents=True, exist_ok=True)
    _remove_empty_batch_temp_table(database_path)
    command.upgrade(config, "head")


def _remove_empty_batch_temp_table(database_path: Path) -> None:
    """Recover only the known empty artifact left by interrupted batch DDL."""
    if not database_path.exists():
        return
    with sqlite3.connect(database_path) as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='_alembic_tmp_batch_items'"
        ).fetchone()
        if not exists:
            return
        count = connection.execute("SELECT COUNT(*) FROM _alembic_tmp_batch_items").fetchone()[0]
        if count:
            raise RuntimeError("non-empty Alembic temporary table requires manual recovery")
        connection.execute("DROP TABLE _alembic_tmp_batch_items")
