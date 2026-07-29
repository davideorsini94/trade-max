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
from collections.abc import Sequence
from datetime import datetime, time, timedelta
from typing import NamedTuple
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

class _Session(NamedTuple):
    """One exchange's continuous-trading window, in that exchange's own timezone."""

    tz: ZoneInfo
    opens: time
    closes: time


# Core continuous-trading hours, per venue family.
_EU_CORE = (time(9, 0), time(17, 30))    # Milan, Xetra, Euronext, Madrid, Zurich…
_LSE_CORE = (time(8, 0), time(16, 30))   # London
_US_CORE = (time(9, 30), time(16, 0))    # NYSE / Nasdaq / Toronto

#: Trading sessions keyed by Yahoo Finance ticker SUFFIX, each in the exchange's
#: OWN timezone so DST is handled by zoneinfo instead of hardcoded offsets.
#:
#: Keying on the suffix (not ``Symbol.exchange``) keeps this intrinsic to the
#: ticker — Yahoo always appends one for a non-US listing, and a bare ticker IS a
#: US listing — and mirrors the convention already used by
#: ``app.data.market._BENCHMARK_SUFFIX_MAP``.
#:
#: These are APPROXIMATE core hours: opening/closing auctions, half-days and
#: public holidays are deliberately not modelled (a holiday just looks like an
#: open market that publishes no new prices). That precision is enough for
#: deciding when an intraday refresh has anything to fetch and when a scheduled
#: analysis is worth an LLM call. Extend the map to cover a new venue.
_EXCHANGE_SESSIONS: dict[str, _Session] = {
    ".MI": _Session(ZoneInfo("Europe/Rome"), *_EU_CORE),
    ".DE": _Session(ZoneInfo("Europe/Berlin"), *_EU_CORE),
    ".F": _Session(ZoneInfo("Europe/Berlin"), *_EU_CORE),
    ".MU": _Session(ZoneInfo("Europe/Berlin"), *_EU_CORE),
    ".BE": _Session(ZoneInfo("Europe/Berlin"), *_EU_CORE),
    ".PA": _Session(ZoneInfo("Europe/Paris"), *_EU_CORE),
    ".AS": _Session(ZoneInfo("Europe/Amsterdam"), *_EU_CORE),
    ".BR": _Session(ZoneInfo("Europe/Brussels"), *_EU_CORE),
    ".LS": _Session(ZoneInfo("Europe/Lisbon"), *_EU_CORE),
    ".MC": _Session(ZoneInfo("Europe/Madrid"), *_EU_CORE),
    ".SW": _Session(ZoneInfo("Europe/Zurich"), *_EU_CORE),
    ".VI": _Session(ZoneInfo("Europe/Vienna"), *_EU_CORE),
    ".ST": _Session(ZoneInfo("Europe/Stockholm"), *_EU_CORE),
    ".CO": _Session(ZoneInfo("Europe/Copenhagen"), time(9, 0), time(17, 0)),
    ".OL": _Session(ZoneInfo("Europe/Oslo"), time(9, 0), time(16, 20)),
    ".HE": _Session(ZoneInfo("Europe/Helsinki"), time(10, 0), time(18, 30)),
    ".L": _Session(ZoneInfo("Europe/London"), *_LSE_CORE),
    ".TO": _Session(ZoneInfo("America/Toronto"), *_US_CORE),
}

#: No recognised suffix means a US listing (NYSE/Nasdaq).
_DEFAULT_SESSION = _Session(ZoneInfo("America/New_York"), *_US_CORE)

# Shared defaults for every job (blueprint section 7.3).
_JOB_DEFAULTS: dict = {
    "max_instances": 1,
    "coalesce": True,
    "misfire_grace_time": 3600,
}

# Pause between scheduled per-symbol analyses (free-tier LLM rate limits).
_INTER_SYMBOL_PAUSE_S = 20.0

# Module-level scheduler; ``dashboard`` reads ``scheduler.running``.
scheduler = AsyncIOScheduler(timezone=APP_TZ)


# --------------------------------------------------------------------------- #
# Market hours
# --------------------------------------------------------------------------- #


def session_for_ticker(ticker: str) -> _Session:
    """Trading session for ``ticker``; the LONGEST matching suffix wins.

    Longest-match matters: ``XYZ.LS`` (Lisbon) must not be read as ``.L``
    (London). An unrecognised or bare ticker falls back to the US session.
    """
    normalized = (ticker or "").strip().upper()
    best_suffix: str | None = None
    for suffix in _EXCHANGE_SESSIONS:
        if normalized.endswith(suffix) and (
            best_suffix is None or len(suffix) > len(best_suffix)
        ):
            best_suffix = suffix
    return _EXCHANGE_SESSIONS[best_suffix] if best_suffix is not None else _DEFAULT_SESSION


def is_symbol_market_open(ticker: str, now: datetime | None = None) -> bool:
    """True when THIS ticker's own exchange is inside its trading window.

    Per-symbol on purpose: a single global window (previously the US session
    only) meant European listings were never refreshed or analysed during the
    European morning, even with their own market wide open.
    """
    session = session_for_ticker(ticker)
    current = now.astimezone(session.tz) if now is not None else datetime.now(session.tz)
    if current.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        return False
    return session.opens <= current.time() <= session.closes


def is_market_open(now: datetime | None = None, tickers: Sequence[str] | None = None) -> bool:
    """True when at least ONE relevant exchange is open.

    ``tickers`` defaults to every active monitored symbol (one small DB read).
    With a mixed EU/US watchlist there is no single "the market", so the
    dashboard badge answers the only honest question: is anything I follow
    trading right now? Fails closed if the symbol list cannot be read.
    """
    if tickers is None:
        try:
            tickers = [ticker for _symbol_id, ticker in _active_symbols()]
        except Exception:
            logger.warning("Lettura dei simboli attivi per is_market_open fallita", exc_info=True)
            return False
    return any(is_symbol_market_open(ticker, now) for ticker in tickers)


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


async def _refresh_prices_for(
    favorites: bool | None,
    interval: str,
    days: int,
    job_id: str,
    *,
    only_when_open: bool = False,
) -> None:
    symbols = await asyncio.to_thread(_active_symbols, favorites)
    for symbol_id, ticker in symbols:
        # Per-symbol gate: each listing follows ITS OWN exchange, so Milan gets
        # refreshed in the European morning and New York in the afternoon.
        if only_when_open and not is_symbol_market_open(ticker):
            continue
        try:
            await asyncio.to_thread(_refresh_symbol_prices, symbol_id, interval, days)
        except Exception:
            logger.exception("%s: price refresh failed for %s (id=%s)", job_id, ticker, symbol_id)


async def job_prices_favorites() -> None:
    """Every 15 min: refresh 1h prices for favorites whose own exchange is open."""
    try:
        await _refresh_prices_for(True, "1h", 30, "prices_favorites", only_when_open=True)
    except Exception:
        logger.exception("prices_favorites job crashed")


async def job_prices_others() -> None:
    """Every 60 min: refresh 1h prices for non-favorites whose exchange is open."""
    try:
        await _refresh_prices_for(False, "1h", 30, "prices_others", only_when_open=True)
    except Exception:
        logger.exception("prices_others job crashed")


async def job_prices_eod() -> None:
    """22:15 Mon-Fri: full 1d history refresh for every active symbol."""
    try:
        await _refresh_prices_for(None, "1d", 730, "prices_eod")
    except Exception:
        logger.exception("prices_eod job crashed")


async def job_sim_book_sync() -> None:
    """22:30 Mon-Fri: allinea il libro simulato alle barre appena scaricate.

    Gira dopo ``prices_eod`` (22:15) perché è proprio con le chiusure del giorno
    che una posizione può scattare in stop, take-profit o scadenza. Deterministico
    e idempotente: se questa passata salta, la successiva — o la chiamata
    opportunistica in testa a ogni analisi — produce esattamente le stesse righe.
    """
    try:
        from app.engine.sim_trader import sync_shadow_book

        def _run() -> dict[str, int]:
            with session_scope() as db:
                return sync_shadow_book(db)

        counters = await asyncio.to_thread(_run)
        logger.info("sim_book_sync completato: %s", counters)
    except Exception:
        logger.exception("sim_book_sync job crashed")


async def _run_scheduled_analyses(
    favorites: bool,
    interval_hours: int,
    job_id: str,
    *,
    only_when_open: bool = False,
) -> None:
    candidates = await asyncio.to_thread(
        _symbols_due_for_analysis, favorites=favorites, interval_hours=interval_hours
    )
    if only_when_open:
        # Analyse a symbol only while its own exchange trades: fresh prices are
        # what makes the LLM call worth paying for.
        candidates = [
            (symbol_id, ticker)
            for symbol_id, ticker in candidates
            if is_symbol_market_open(ticker)
        ]
    if not candidates:
        return
    # Lazy import: the engine package is built separately and may be absent.
    from app.engine.orchestrator import run_analysis

    for index, (symbol_id, ticker) in enumerate(candidates):
        if index > 0:
            # Pace scheduled runs: back-to-back symbols (6 LLM calls each) are
            # exactly the burst shape that trips free-tier provider rate limits.
            await asyncio.sleep(_INTER_SYMBOL_PAUSE_S)
        try:
            await run_analysis(symbol_id, trigger="SCHEDULED")
        except Exception:
            logger.exception("%s: run_analysis failed for %s (id=%s)", job_id, ticker, symbol_id)


async def job_analysis_favorites() -> None:
    """Hourly tick: analyze favorites past their interval whose exchange is open."""
    try:
        interval_hours = _get_setting_int("favorites_analysis_interval_hours", 24)
        await _run_scheduled_analyses(
            True, interval_hours, "analysis_favorites", only_when_open=True
        )
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
        job_sim_book_sync,
        CronTrigger(day_of_week="mon-fri", hour=22, minute=30, timezone=APP_TZ),
        id="sim_book_sync",
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
