"""Database backbone: engine, session factory, declarative Base and helpers.

Single-process, file-backed SQLite. ``check_same_thread=False`` lets the FastAPI
threadpool and the APScheduler worker threads share the engine. A ``connect``
listener turns on SQLite foreign-key enforcement so that the ``ondelete=CASCADE``
constraints declared on the models are actually honoured at the database level.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model in ``app.models``."""


def _build_sqlite_url(db_path: str) -> str:
    """Turn a filesystem path (or explicit sqlite URL) into a SQLAlchemy URL."""
    if db_path.startswith("sqlite:"):
        return db_path
    # ``sqlite:///relative/path`` and ``sqlite:////absolute/path`` are both valid;
    # prefixing the raw path with three slashes yields the correct form for each.
    return f"sqlite:///{db_path}"


_settings = get_settings()
DATABASE_URL = _build_sqlite_url(_settings.db_path)

engine: Engine = create_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record) -> None:  # noqa: ANN001
    """Enable foreign-key enforcement on every new SQLite connection."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    future=True,
    class_=Session,
)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a session and closing it afterwards."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on error, always close.

    Use in background jobs / services that manage their own transaction lifetime
    (schedulers, orchestrator, evaluator) rather than the request-scoped
    ``get_db`` dependency.
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
