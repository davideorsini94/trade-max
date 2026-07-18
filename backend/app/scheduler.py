"""APScheduler jobs and market-hours helper (blueprint section 7.3).

A single module-level ``AsyncIOScheduler`` (timezone Europe/Rome) drives every
recurring backend task: price refreshes, scheduled analyses, the weekly
evaluation and periodic macro-news fetches. The dashboard health endpoint reads
``scheduler.running`` and ``is_market_open()`` from this module.

Robustness contract
--------------------
* Every job body is wrapped in try/except and logs on failure — a scheduler
  thread must never die.
* Per-symbol loops isolate each symbol: one bad ticker never aborts the batch.
* ``run_analysis`` / ``run_weekly_evaluation`` are imported lazily inside the
  jobs. The engine package is built separately and may not exist at import
  time; a top-level import would break the whole module.
* Blocking work (yfinance, feedparser) runs via ``asyncio.to_thread``; each such
  helper opens its own ``session_scope`` so no Session is shared across threads.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.data.market import market_data_service
from app.data.news import news_service
from app.db import session_scope
from app.models import AnalysisRun, AppSettings, Symbol

logger = logging.getLogger(__name__)

APP_TZ = ZoneInfo("Europe/Rome")

# US session in local (Europe/Rome) terms; sufficient for v1 (README-documented).
_MARKET_OPEN = time(15, 30)
_MARKET_CLOSE = time(22, 0)

# Shared defaults for every job (blueprint section 7.3).
_JOB_DEFAULTS: dict = {
    "max_instances": 1,
    "coalesce": True,
    "misfire_grace_time": 3600,
}

# Module-level scheduler; ``dashboard`` reads ``scheduler.running``.
scheduler = AsyncIOScheduler(timezone=APP_TZ)


# --------------------------------------------------------------------------- #
# Market hours
# --------------------------------------------------------------------------- #


def is_market_open(now: datetime | None = None) -> bool:
    """True on weekdays between 15:30 and 22:00 Europe/Rome."""
    current = now.astimezone(APP_TZ) if now is not None else datetime.now(APP_TZ)
    if current.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        return False
    return _MARKET_OPEN <= current.time() <= _MARKET_CLOSE


# --------------------------------------------------------------------------- #
# Synchronous DB / network helpers (run via asyncio.to_thread)
# --------------------------------------------------------------------------- #


def _active_symbols(favorites: bool | None = None) -> list[tuple[int, str]]:
    """(id, ticker) for active symbols, optionally filtered by favorite flag."""
    with session_scope() as db:
        stmt = select(Symbol.id, Symbol.ticker).where(Symbol.is_active.is_(True))
        if favorites is True:
            stmt = stmt.where(Symbol.is_favorite.is_(True))
        elif favorites is False:
            stmt = stmt.where(Symbol.is_favorite.is_(False))
        return [(row[0], row[1]) for row in db.execute(stmt).all()]


def _refresh_symbol_prices(symbol_id: int, interval: str, days: int) -> int:
    """Refresh one symbol's price history in a self-contained session."""
    with session_scope() as db:
        symbol = db.get(Symbol, symbol_id)
        if symbol is None:
            return 0
        return market_data_service.refresh_prices(db, symbol, interval, days)


def _get_setting_int(field: str, default: int) -> int:
    """Read an integer AppSettings field (id=1), falling back to ``default``."""
    try:
        with session_scope() as db:
            settings = db.get(AppSettings, 1)
            if settings is None:
                return default
            value = getattr(settings, field, None)
            return int(value) if value is not None else default
    except Exception:
        logger.exception("Failed to read setting %s", field)
        return default


def _symbols_due_for_analysis(*, favorites: bool, interval_hours: int) -> list[tuple[int, str]]:
    """Active symbols whose latest COMPLETED run is older than ``interval_hours``.

    Symbols that have never completed a run are always due.
    """
    threshold = datetime.utcnow() - timedelta(hours=interval_hours)
    due: list[tuple[int, str]] = []
    with session_scope() as db:
        symbols = (
            db.execute(
                select(Symbol).where(
                    Symbol.is_active.is_(True),
                    Symbol.is_favorite.is_(favorites),
                )
            )
            .scalars()
            .all()
        )
        for symbol in symbols:
            last_finished = db.execute(
                select(AnalysisRun.finished_at)
                .where(
                    AnalysisRun.symbol_id == symbol.id,
                    AnalysisRun.status == "COMPLETED",
                )
                .order_by(AnalysisRun.finished_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if last_finished is None or last_finished < threshold:
                due.append((symbol.id, symbol.ticker))
    return due


def _fetch_macro_news() -> None:
    """Refresh the cached MACRO feeds in a self-contained session."""
    with session_scope() as db:
        news_service.fetch_macro(db)


def _refresh_universe_stats() -> int:
    """Refresh the curated universe's market stats in a self-contained session.

    ``refresh_universe`` is imported lazily so this module keeps importing even
    if the data package is being reshaped, mirroring the engine/evaluation jobs.
    """
    from app.data.universe import refresh_universe

    with session_scope() as db:
        return refresh_universe(db)


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #


async def _refresh_prices_for(favorites: bool, interval: str, days: int, job_id: str) -> None:
    symbols = await asyncio.to_thread(_active_symbols, favorites)
    for symbol_id, ticker in symbols:
        try:
            await asyncio.to_thread(_refresh_symbol_prices, symbol_id, interval, days)
        except Exception:
            logger.exception("%s: price refresh failed for %s (id=%s)", job_id, ticker, symbol_id)


async def job_prices_favorites() -> None:
    """Every 15 min while the market is open: refresh 1h prices for favorites."""
    try:
        if not is_market_open():
            return
        await _refresh_prices_for(True, "1h", 30, "prices_favorites")
    except Exception:
        logger.exception("prices_favorites job crashed")


async def job_prices_others() -> None:
    """Every 60 min while the market is open: refresh 1h prices for non-favorites."""
    try:
        if not is_market_open():
            return
        await _refresh_prices_for(False, "1h", 30, "prices_others")
    except Exception:
        logger.exception("prices_others job crashed")


async def job_prices_eod() -> None:
    """22:15 Mon-Fri: full 1d history refresh for every active symbol."""
    try:
        await _refresh_prices_for(None, "1d", 730, "prices_eod")
    except Exception:
        logger.exception("prices_eod job crashed")


async def _run_scheduled_analyses(favorites: bool, interval_hours: int, job_id: str) -> None:
    candidates = await asyncio.to_thread(
        _symbols_due_for_analysis, favorites=favorites, interval_hours=interval_hours
    )
    if not candidates:
        return
    # Lazy import: the engine package is built separately and may be absent.
    from app.engine.orchestrator import run_analysis

    for symbol_id, ticker in candidates:
        try:
            await run_analysis(symbol_id, trigger="SCHEDULED")
        except Exception:
            logger.exception("%s: run_analysis failed for %s (id=%s)", job_id, ticker, symbol_id)


async def job_analysis_favorites() -> None:
    """Hourly tick (market open only): analyze favorites past their interval."""
    try:
        if not is_market_open():
            return
        interval_hours = _get_setting_int("favorites_analysis_interval_hours", 4)
        await _run_scheduled_analyses(True, interval_hours, "analysis_favorites")
    except Exception:
        logger.exception("analysis_favorites job crashed")


async def job_analysis_others() -> None:
    """16:00 daily: analyze non-favorites past their (longer) interval."""
    try:
        interval_hours = _get_setting_int("others_analysis_interval_hours", 24)
        await _run_scheduled_analyses(False, interval_hours, "analysis_others")
    except Exception:
        logger.exception("analysis_others job crashed")


async def job_weekly_evaluation() -> None:
    """Sunday 18:00: run the weekly evaluation (skip quietly if one is running)."""
    try:
        from app.evaluation.evaluator import run_weekly_evaluation

        await run_weekly_evaluation()
    except RuntimeError as exc:
        logger.info("weekly_evaluation skipped: %s", exc)
    except Exception:
        logger.exception("weekly_evaluation job crashed")


async def job_news_refresh() -> None:
    """Every 30 min: refresh the cached MACRO news feeds."""
    try:
        await asyncio.to_thread(_fetch_macro_news)
    except Exception:
        logger.exception("news_refresh job crashed")


async def job_universe_refresh() -> None:
    """Every 6h: refresh the curated stock universe ("Universo titoli") stats."""
    try:
        await asyncio.to_thread(_refresh_universe_stats)
    except Exception:
        logger.exception("universe_refresh job crashed")


# --------------------------------------------------------------------------- #
# Wiring / lifecycle
# --------------------------------------------------------------------------- #


def setup_scheduler() -> AsyncIOScheduler:
    """Register every job on the module scheduler (idempotent)."""
    scheduler.add_job(
        job_prices_favorites,
        IntervalTrigger(minutes=15),
        id="prices_favorites",
        replace_existing=True,
        **_JOB_DEFAULTS,
    )
    scheduler.add_job(
        job_prices_others,
        IntervalTrigger(minutes=60),
        id="prices_others",
        replace_existing=True,
        **_JOB_DEFAULTS,
    )
    scheduler.add_job(
        job_prices_eod,
        CronTrigger(day_of_week="mon-fri", hour=22, minute=15, timezone=APP_TZ),
        id="prices_eod",
        replace_existing=True,
        **_JOB_DEFAULTS,
    )
    scheduler.add_job(
        job_analysis_favorites,
        IntervalTrigger(hours=1),
        id="analysis_favorites",
        replace_existing=True,
        **_JOB_DEFAULTS,
    )
    scheduler.add_job(
        job_analysis_others,
        CronTrigger(hour=16, minute=0, timezone=APP_TZ),
        id="analysis_others",
        replace_existing=True,
        **_JOB_DEFAULTS,
    )
    scheduler.add_job(
        job_weekly_evaluation,
        CronTrigger(day_of_week="sun", hour=18, minute=0, timezone=APP_TZ),
        id="weekly_evaluation",
        replace_existing=True,
        **_JOB_DEFAULTS,
    )
    scheduler.add_job(
        job_news_refresh,
        IntervalTrigger(minutes=30),
        id="news_refresh",
        replace_existing=True,
        **_JOB_DEFAULTS,
    )
    scheduler.add_job(
        job_universe_refresh,
        IntervalTrigger(hours=6),
        id="universe_refresh",
        replace_existing=True,
        **_JOB_DEFAULTS,
    )
    return scheduler


def start_scheduler() -> AsyncIOScheduler:
    """Register jobs (if needed) and start the scheduler (FastAPI lifespan)."""
    if not scheduler.get_jobs():
        setup_scheduler()
    if not scheduler.running:
        scheduler.start()
        logger.info("Scheduler started (%d jobs, tz=%s)", len(scheduler.get_jobs()), APP_TZ)
    return scheduler


def shutdown_scheduler(wait: bool = False) -> None:
    """Stop the scheduler on shutdown (FastAPI lifespan)."""
    if scheduler.running:
        scheduler.shutdown(wait=wait)
        logger.info("Scheduler stopped")


def scheduler_running() -> bool:
    """Whether the scheduler is currently running."""
    return bool(scheduler.running)
