"""Shared pytest fixtures for the API test suite.

Builds the FastAPI app *without* the production lifespan (no scheduler
startup, no ``app.main`` import at all): a bare ``FastAPI()`` instance gets
only ``api_router`` mounted, and the ``get_db`` dependency is overridden to
hand out sessions bound to a fresh temp-file SQLite database per test. This
keeps the API test suite fully offline and isolated from ``trademax.db``.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

# Importing app.models registers every mapped class on Base.metadata so
# create_all() below builds the full schema, not just the tables referenced
# directly by this test module.
import app.models  # noqa: F401
from app.api import api_router
from app.api.deps import get_db
from app.db import Base


@pytest.fixture()
def db_engine(tmp_path) -> Generator[Engine, None, None]:
    """A fresh temp-file SQLite engine with the full schema created."""
    db_path = tmp_path / "test_trademax.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    Base.metadata.create_all(bind=engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def db_session_factory(db_engine: Engine) -> sessionmaker[Session]:
    """A sessionmaker bound to the per-test temp-file engine."""
    return sessionmaker(
        bind=db_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        future=True,
        class_=Session,
    )


@pytest.fixture()
def app(db_session_factory: sessionmaker[Session]) -> FastAPI:
    """A bare FastAPI app with only ``api_router`` mounted, no real lifespan."""
    test_app = FastAPI()
    test_app.include_router(api_router)

    def _override_get_db() -> Generator[Session, None, None]:
        db = db_session_factory()
        try:
            yield db
        finally:
            db.close()

    test_app.dependency_overrides[get_db] = _override_get_db
    return test_app


@pytest.fixture()
def client(app: FastAPI) -> Generator[TestClient, None, None]:
    """A ``TestClient`` for the isolated test app."""
    with TestClient(app) as test_client:
        yield test_client
