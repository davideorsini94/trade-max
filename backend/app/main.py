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
from app.api import api_router
from app.config import get_settings
from app.db import Base, engine, session_scope
from app.models import AppSettings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def _seed_settings() -> None:
    """Ensure the single ``app_settings`` row (id=1) exists."""
    with session_scope() as db:
        if db.get(AppSettings, 1) is None:
            db.add(AppSettings(id=1))
            logger.info("Seeded default app_settings (id=1)")


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    _seed_settings()
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
        return FileResponse(FRONTEND_DIST / "index.html")
