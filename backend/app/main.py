"""FastAPI application entrypoint.

Wires together the API router, CORS, the production lifespan (schema creation,
settings seed, scheduler start/stop) and — when a production build exists —
the static frontend from ``frontend/dist`` with an SPA catch-all.

Run from the ``backend/`` directory: ``uvicorn app.main:app``.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import app.models  # noqa: F401  (register every mapped class on Base.metadata)
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.engine import Engine

from app.api import api_router
from app.config import get_settings
from app.db import Base, engine, session_scope
from app.models import AnalysisRun, AppSettings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


#: Columns added to existing tables after their first release, keyed by table
#: name. ``create_all`` only creates *missing tables*, never new columns on an
#: existing one, so a DB created by an earlier version lacks these. This tiny
#: additive migration fills the gap (SQLite ``ADD COLUMN`` is cheap and safe).
#: Extend this map when a new nullable column is added to an existing table.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "llm_provider_settings": {"ollama_base_url": "TEXT"},
    "recommendations": {"features_json": "TEXT"},
}


def _ensure_columns(bind: Engine) -> None:
    """Additively backfill columns missing from pre-existing tables.

    For each table in :data:`_ADDED_COLUMNS`, read its live column set via
    ``PRAGMA table_info`` and ``ALTER TABLE ... ADD COLUMN`` any that are absent.
    A brand-new DB (built by ``create_all``) already has every column, so this is
    a no-op there; it only matters for databases created before the column existed.
    Idempotent and safe to run on every boot.
    """
    with bind.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            existing = {
                row[1]  # PRAGMA table_info columns: (cid, name, type, ...)
                for row in conn.execute(text(f"PRAGMA table_info({table})"))
            }
            if not existing:
                # Table does not exist yet (fresh DB before create_all, or an
                # unrelated table name); create_all / the model definition owns it.
                continue
            for name, col_type in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}"))
                    logger.info("Migrazione DB: aggiunta colonna %s.%s", table, name)


def _seed_settings() -> None:
    """Ensure the single ``app_settings`` row (id=1) exists."""
    with session_scope() as db:
        if db.get(AppSettings, 1) is None:
            db.add(AppSettings(id=1))
            logger.info("Seeded default app_settings (id=1)")


def _fail_orphaned_runs() -> None:
    """Mark runs left PENDING/RUNNING by a previous process as FAILED.

    Runs execute as in-process asyncio tasks: none can survive a restart, so
    anything still in-flight at boot is an orphan. Without this, the UI would
    show "Analisi in corso…" forever for a run that no process is executing.
    """
    with session_scope() as db:
        stale = (
            db.execute(select(AnalysisRun).where(AnalysisRun.status.in_(("PENDING", "RUNNING"))))
            .scalars()
            .all()
        )
        for run in stale:
            run.status = "FAILED"
            run.error = "Analisi interrotta da un riavvio dell'applicazione. Riprova."
            run.finished_at = datetime.utcnow()
        if stale:
            logger.info("Marcate FAILED %d run orfane di processi precedenti", len(stale))


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    _ensure_columns(engine)
    _seed_settings()
    _fail_orphaned_runs()
    from app.scheduler import setup_scheduler, shutdown_scheduler, start_scheduler

    setup_scheduler()
    start_scheduler()
    logger.info("TradeMax avviato: scheduler attivo")
    try:
        yield
    finally:
        shutdown_scheduler()
        logger.info("TradeMax arrestato")


app = FastAPI(title="TradeMax", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


if FRONTEND_DIST.is_dir():
    app.mount(
        "/assets",
        StaticFiles(directory=FRONTEND_DIST / "assets"),
        name="assets",
    )

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_catch_all(full_path: str) -> FileResponse:
        """Serve the SPA: real files if they exist, index.html for app routes."""
        candidate = (FRONTEND_DIST / full_path).resolve()
        if (
            full_path
            and candidate.is_file()
            and candidate.is_relative_to(FRONTEND_DIST.resolve())
        ):
            return FileResponse(candidate)
        # index.html references the build's content-hashed JS/CSS filenames
        # (e.g. index-<hash>.js), which change on every deploy. Without an
        # explicit Cache-Control, browsers apply heuristic caching to this
        # response (it has Last-Modified/ETag but no directive), so a tab left
        # open across a rebuild keeps running the OLD bundle indefinitely —
        # new features/fixes silently don't appear until a manual hard reload.
        # "no-cache" forces revalidation on every load (cheap: a 304 when
        # unchanged) instead of disabling caching outright.
        return FileResponse(
            FRONTEND_DIST / "index.html", headers={"Cache-Control": "no-cache"}
        )
