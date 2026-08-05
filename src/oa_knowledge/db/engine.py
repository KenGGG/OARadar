from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session


def create_db_engine(path: Path) -> Engine:
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


@contextmanager
def db_session(settings: Any) -> Iterator[Session]:
    """Yield a SQLAlchemy Session bound to a fresh engine for ``settings``.

    Centralizes engine creation and disposal so call sites no longer repeat the
    ``create_db_engine`` + ``with Session`` + ``engine.dispose()`` boilerplate.
    """
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    session = Session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
