"""End-to-end multi-agent analysis orchestrator (blueprint section 5.5).

``run_analysis`` drives the full pipeline for a single symbol:

1. mark the ``AnalysisRun`` RUNNING;
2. refresh market data (prices, indicators, fundamentals, news) through the
   synchronous data-layer services, wrapped in ``asyncio.to_thread``;
3. build one :class:`AgentContext` per analyst (with that agent's active
   lessons) and fan out over the analysts with
   ``asyncio.gather(return_exceptions=True)``, persisting one ``Analysis`` row
   per actor (including FAILED ones);
4. abort the run as FAILED once all-but-one of the analysts failed;
5. run the synthesizer, then the adversarial validator, persisting each;
6. apply the deterministic :class:`~app.engine.policy.PolicyEngine`;
7. persist the final ``Recommendation`` and mark the run COMPLETED.

Concurrency: a per-symbol in-memory ``asyncio.Lock`` serializes runs;
``is_running(symbol_id)`` reports whether one is in flight (the API answers 409
based on it). The whole body is wrapped so any exception marks the run FAILED
and is logged — ``run_analysis`` never re-raises. It always returns the run id.

DB sessions are managed here via ``app.db.session_scope``; work is committed
incrementally (per phase) so the ``GET /api/runs/{id}`` polling endpoint sees
per-agent progress while the run is still in flight.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pandas as pd
from sqlalchemy import select

from app.agents import (
    ANALYST_AGENTS,
    AgentContext,
    AgentResult,
    RiskValidatorAgent,
    SynthesizerAgent,
)
from app.data.indicators import compute_all
from app.data.market import market_data_service
from app.data.news import news_service
from app.db import session_scope
from app.engine.policy import MarketMetrics, PolicyEngine
from app.models import Analysis, AnalysisRun, AppSettings, Recommendation, Symbol
from app.schemas import Action, RunStatus, SymbolOut

logger = logging.getLogger(__name__)

#: Persisted error strings feed the UI; keep them bounded.
_MAX_ERROR_CHARS = 2000

#: A run is aborted once at least this many analysts fail (blueprint §5.5 step 4):
#: one less than the total number of registered analysts (so with 5 analysts the
#: run still proceeds while at least one analyst succeeded).
_MIN_FAILED_ANALYSTS_TO_ABORT = len(ANALYST_AGENTS) - 1

#: Prompt-slimming caps (persisted data is unaffected; these bound only what the
#: LLM actors actually read, to minimise token cost without losing signal).
_MAX_NEWS_ITEMS = 10
_MAX_NEWS_SUMMARY_CHARS = 280
_MAX_RECENT_CLOSES = 20

#: Core fundamentals fields; when ALL are None the title has no usable
#: fundamentals (e.g. an ETF), so the fundamentals agent is deterministically
#: skipped rather than paying for an LLM call over an empty payload.
_FUNDAMENTALS_CORE_FIELDS: tuple[str, ...] = ("pe", "forward_pe", "eps", "market_cap", "beta")

#: Concentration-risk correlations: 90-day daily-return correlation between the
#: analysed symbol and each currently-open BUY position (blueprint §5.4 addendum).
#: A pair is dropped — never estimated — when fewer than _CORRELATION_MIN_OVERLAP
#: daily returns overlap; a correlation at/above _CORRELATION_ALERT_THRESHOLD
#: raises the validator's concentration-risk flag.
_CORRELATION_SESSIONS = 90            # rendimenti giornalieri sulle ultime 90 sessioni
_CORRELATION_MIN_OVERLAP = 60         # minimo di rendimenti sovrapposti per fidarsi della corr
_CORRELATION_ALERT_THRESHOLD = 0.75   # soglia di rischio concentrazione
_MAX_CORRELATIONS_IN_PROMPT = 5

#: Core sentiment fields; when ALL are None AND there is no ratings_trend and no
#: insider_transactions, the title has no usable sentiment data (common for many
#: non-US tickers), so the sentiment agent is deterministically skipped.
_SENTIMENT_CORE_FIELDS: tuple[str, ...] = (
    "recommendation_mean",
    "institutions_pct_held",
    "short_percent_of_float",
)

#: Provider marker persisted for deterministic (no-LLM) analyst outputs.
_DETERMINISTIC_PROVIDER = "deterministic"

#: Deterministic outputs used when there is nothing for an analyst to reason
#: about. They share the common analyst schema so they flow into the synthesizer
#: exactly like real LLM outputs, and are always persisted with status OK.
_DET_CORPORATE_NEWS: dict[str, Any] = {
    "stance": "NEUTRAL",
    "signal": 0.0,
    "confidence": 0.2,
    "key_points": [],
    "risks": [],
    "data_quality": "POOR",
    "summary_it": (
        "Nessuna notizia societaria rilevante nelle ultime 72 ore dalle fonti "
        "ufficiali monitorate."
    ),
}
_DET_MACRO_NEWS: dict[str, Any] = {
    "stance": "NEUTRAL",
    "signal": 0.0,
    "confidence": 0.2,
    "key_points": [],
    "risks": [],
    "data_quality": "POOR",
    "summary_it": (
        "Nessuna notizia macroeconomica rilevante nelle ultime 72 ore dalle fonti "
        "ufficiali monitorate."
    ),
}
_DET_FUNDAMENTALS: dict[str, Any] = {
    "stance": "NEUTRAL",
    "signal": 0.0,
    "confidence": 0.2,
    "key_points": [],
    "risks": [],
    "data_quality": "POOR",
    "summary_it": (
        "Dati fondamentali non disponibili per questo titolo (es. ETF o dati "
        "mancanti dal provider)."
    ),
}
_DET_SENTIMENT: dict[str, Any] = {
    "stance": "NEUTRAL",
    "signal": 0.0,
    "confidence": 0.2,
    "consensus": "UNKNOWN",
    "key_points": [],
    "risks": [],
    "data_quality": "POOR",
    "summary_it": (
        "Nessun dato disponibile su consenso degli analisti, operazioni degli insider "
        "o investitori istituzionali per questo titolo dalle fonti monitorate."
    ),
}


def _slim_news(items: Any) -> list[dict]:
    """Cap news for the prompt: at most 10 items, each summary <= 280 chars.

    Persisted ``news_items`` rows are untouched — this only bounds the copy that
    reaches an analyst prompt.
    """
    slimmed: list[dict] = []
    for item in (items or [])[:_MAX_NEWS_ITEMS]:
        if not isinstance(item, dict):
            continue
        entry = dict(item)
        summary = entry.get("summary")
        if isinstance(summary, str) and len(summary) > _MAX_NEWS_SUMMARY_CHARS:
            entry["summary"] = summary[:_MAX_NEWS_SUMMARY_CHARS]
        slimmed.append(entry)
    return slimmed


def _fundamentals_are_empty(fundamentals: Any) -> bool:
    """True when every core fundamentals field is None (or the dict is absent)."""
    if not isinstance(fundamentals, dict):
        return True
    return all(fundamentals.get(field) is None for field in _FUNDAMENTALS_CORE_FIELDS)


def _sentiment_is_empty(sentiment: Any) -> bool:
    """True when there is nothing for the sentiment analyst to reason about.

    That is: every core field (recommendation_mean, institutions_pct_held,
    short_percent_of_float) is None AND there is no ratings_trend AND no
    insider_transactions (common for many non-US tickers).
    """
    if not isinstance(sentiment, dict):
        return True
    core_all_none = all(sentiment.get(field) is None for field in _SENTIMENT_CORE_FIELDS)
    return core_all_none and not sentiment.get("ratings_trend") and not sentiment.get(
        "insider_transactions"
    )


def _deterministic_analyst_outputs(data: dict) -> dict[str, dict]:
    """Analyst outputs that can be produced WITHOUT an LLM call for this run.

    Returns ``{agent_name: output}`` for each analyst that has nothing to analyse:
    empty ``corporate_news``/``macro_news``, or fundamentals with all core numeric
    fields missing. The technical analyst is never skipped. Each returned output
    is a fresh copy of the shared analyst schema (status OK downstream).
    """
    skips: dict[str, dict] = {}
    if not data.get("macro_news"):
        skips["macro_news"] = dict(_DET_MACRO_NEWS)
    if not data.get("corporate_news"):
        skips["corporate_news"] = dict(_DET_CORPORATE_NEWS)
    if _fundamentals_are_empty(data.get("fundamentals")):
        skips["fundamentals"] = dict(_DET_FUNDAMENTALS)
    if _sentiment_is_empty(data.get("sentiment")):
        skips["sentiment"] = dict(_DET_SENTIMENT)
    return skips


def _slim_analyst_outputs(
    analyst_outputs: dict[str, dict | None],
) -> dict[str, dict | None]:
    """Drop the UI-only ``summary_it`` from each analyst output for the actors.

    The synthesizer and validator reason on stance/signal/confidence/key_points/
    risks/data_quality and the agent-specific extra field — never on the Italian
    ``summary_it`` (~150 tokens each). Persisted ``Analysis`` rows keep the full
    output; this slimmed copy is what actually enters the prompts.
    """
    slimmed: dict[str, dict | None] = {}
    for name, output in analyst_outputs.items():
        if isinstance(output, dict):
            slimmed[name] = {key: value for key, value in output.items() if key != "summary_it"}
        else:
            slimmed[name] = output
    return slimmed


# --------------------------------------------------------------------------- #
# Per-symbol concurrency
# --------------------------------------------------------------------------- #

_symbol_locks: dict[int, asyncio.Lock] = {}


def _get_lock(symbol_id: int) -> asyncio.Lock:
    """Return (creating if needed) the in-memory lock for ``symbol_id``."""
    lock = _symbol_locks.get(symbol_id)
    if lock is None:
        lock = asyncio.Lock()
        _symbol_locks[symbol_id] = lock
    return lock


def is_running(symbol_id: int) -> bool:
    """Whether an analysis is currently in flight for ``symbol_id``."""
    lock = _symbol_locks.get(symbol_id)
    return lock is not None and lock.locked()


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _err_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]


def _get_llm() -> Any:
    """Return the process-wide ``LLMClient`` (lazy import to avoid import cycles)."""
    from app.api.deps import get_llm_client

    return get_llm_client()


def _load_lessons(agent_name: str) -> list[str]:
    """Active lessons for ``agent_name`` (empty until the evaluation module lands).

    The ``app.evaluation`` package is built in parallel; import it lazily and
    fall back to no lessons if it is not present yet (blueprint §10 note).
    """
    try:
        from app.evaluation.feedback import get_active_lessons
    except ImportError:
        return []
    try:
        with session_scope() as db:
            lessons = get_active_lessons(db, agent_name, limit=5)
        return list(lessons) if lessons else []
    except Exception:  # pragma: no cover - defensive
        logger.warning("Failed to load lessons for agent %s", agent_name, exc_info=True)
        return []


def _action_signal(action: Any, confidence: Any) -> float:
    """Map a synthesizer action to a signed signal: BUY=+conf, SELL=-conf, HOLD=0."""
    conf = 0.0
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
        conf = float(confidence)
    value = str(action).strip().upper() if action is not None else ""
    if value == Action.BUY.value:
        return conf
    if value == Action.SELL.value:
        return -conf
    return 0.0


def _safe_fail(run_id: int | None, message: str) -> None:
    """Best-effort transition of a run to FAILED with an Italian error message."""
    if run_id is None:
        return
    try:
        with session_scope() as db:
            run = db.get(AnalysisRun, run_id)
            if run is not None:
                run.status = RunStatus.FAILED.value
                run.error = message[:_MAX_ERROR_CHARS]
                run.finished_at = datetime.utcnow()
    except Exception:  # pragma: no cover - defensive
        logger.exception("Impossibile marcare come FAILED la run %s", run_id)


def _build_metrics(price_summary: dict, latest: dict) -> MarketMetrics:
    return MarketMetrics(
        last_close=price_summary.get("close"),
        atr14=latest.get("atr14"),
        sma50=latest.get("sma50"),
        sma200=latest.get("sma200"),
        rsi14=latest.get("rsi14"),
        drawdown_90d_pct=latest.get("drawdown_90d_pct"),
        volatility_30d_pct=latest.get("volatility_30d_pct"),
    )


def _build_risk_metrics(
    metrics: MarketMetrics,
    fundamentals: dict,
    sentiment: dict,
    market_regime: dict,
    correlations: list[dict],
) -> dict:
    """Deterministic risk metrics for the validator (blueprint §5.4/§5.5).

    A slim, computed slice — not the full ``market_regime`` block: only the three
    regime fields most relevant to sizing/stops risk (equity volatility and the
    credit-spread trend) are surfaced here so the validator always sees them, even
    when the macro_news agent is deterministically skipped. ``correlations`` is
    the 90-day return correlation vs. every open BUY position (see
    :func:`_open_position_correlations`); empty when there are no open positions
    or not enough overlapping history — never estimated.
    """
    atr_pct = None
    if metrics.atr14 is not None and metrics.last_close:
        atr_pct = metrics.atr14 / metrics.last_close * 100.0
    distance = None
    if metrics.last_close is not None and metrics.sma200:
        distance = (metrics.last_close - metrics.sma200) / metrics.sma200 * 100.0
    beta = fundamentals.get("beta") if isinstance(fundamentals, dict) else None
    days_to_next_earnings = (
        sentiment.get("days_to_earnings") if isinstance(sentiment, dict) else None
    )
    regime = market_regime if isinstance(market_regime, dict) else {}
    max_correlation = max((c["correlation"] for c in correlations), default=None)
    return {
        "atr_pct": atr_pct,
        "drawdown_90d_pct": metrics.drawdown_90d_pct,
        "beta": beta,
        "distance_from_sma200_pct": distance,
        "days_to_next_earnings": days_to_next_earnings,
        "vix_level": regime.get("vix_level"),
        "vix_change_30d_pct": regime.get("vix_change_30d_pct"),
        "credit_hyg_lqd_ratio_change_30d_pct": regime.get(
            "credit_hyg_lqd_ratio_change_30d_pct"
        ),
        "open_position_correlations_90d": correlations or None,
        "max_open_position_correlation_90d": max_correlation,
        "correlation_alert": (
            max_correlation is not None and max_correlation >= _CORRELATION_ALERT_THRESHOLD
        ),
    }


def _open_buy_positions(db, current_symbol_id: int) -> list[tuple[Symbol, Recommendation]]:
    """The *other* active symbols whose most recent BUY is still open.

    A position is "open" when a symbol's most recent BUY recommendation is not
    followed by a later SELL for the same symbol (blueprint §5.5 / rule 10).
    """
    positions: list[tuple[Symbol, Recommendation]] = []
    symbols = (
        db.execute(
            select(Symbol).where(Symbol.is_active.is_(True), Symbol.id != current_symbol_id)
        )
        .scalars()
        .all()
    )
    for sym in symbols:
        last_buy = db.execute(
            select(Recommendation)
            .where(
                Recommendation.symbol_id == sym.id,
                Recommendation.action == Action.BUY.value,
            )
            .order_by(Recommendation.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if last_buy is None:
            continue
        later_sell = db.execute(
            select(Recommendation)
            .where(
                Recommendation.symbol_id == sym.id,
                Recommendation.action == Action.SELL.value,
                Recommendation.created_at > last_buy.created_at,
            )
            .limit(1)
        ).scalar_one_or_none()
        if later_sell is None:
            positions.append((sym, last_buy))
    return positions


def _compute_open_allocation(db, current_symbol_id: int) -> float:
    """Sum of still-open BUY allocations across the *other* active symbols."""
    return sum(
        float(rec.allocation_pct or 0.0) for _sym, rec in _open_buy_positions(db, current_symbol_id)
    )


def _daily_returns_by_session(df: pd.DataFrame) -> pd.Series:
    """Daily % returns over the last ``_CORRELATION_SESSIONS`` sessions, indexed by session date.

    ``price_history.ts`` is midnight *local exchange time* converted to naive
    UTC, so a non-US exchange can land on e.g. 22:00-23:00 UTC of the PRIOR
    calendar day. Joining two such series on the raw ``ts`` would misalign
    cross-exchange pairs by a day, so the index is first normalized to a
    session DATE via a +12h shift (safely inside any exchange's UTC offset)
    before computing returns — this is what lets ``Series.corr`` (which aligns
    by index) actually line up same-day sessions across exchanges.
    """
    if not isinstance(df, pd.DataFrame) or df.empty or "close" not in df.columns:
        return pd.Series(dtype=float)
    closes = df["close"].dropna().tail(_CORRELATION_SESSIONS + 1)
    if closes.empty:
        return pd.Series(dtype=float)
    session_dates = [(ts + timedelta(hours=12)).date() for ts in closes.index]
    closes = pd.Series(closes.values, index=pd.Index(session_dates))
    return closes.pct_change().dropna()


def _open_position_correlations(db, current_symbol_id: int, df_daily: pd.DataFrame) -> list[dict]:
    """90-day daily-return correlation vs. every currently-open BUY position.

    Deterministic and DB-only (no network): reuses the already-fetched
    ``price_history`` for every open position. A pair is DROPPED — never
    estimated — when fewer than ``_CORRELATION_MIN_OVERLAP`` daily returns
    overlap between the two symbols. Results are sorted by correlation
    (descending) and capped at ``_MAX_CORRELATIONS_IN_PROMPT`` to bound the
    validator's prompt cost.
    """
    ret_current = _daily_returns_by_session(df_daily)
    if ret_current.empty:
        return []

    results: list[dict] = []
    for sym, _rec in _open_buy_positions(db, current_symbol_id):
        other_df = market_data_service.get_history_df(db, sym.id, interval="1d", days=180)
        ret_other = _daily_returns_by_session(other_df)
        if ret_other.empty:
            continue
        corr = ret_current.corr(ret_other, min_periods=_CORRELATION_MIN_OVERLAP)
        if pd.isna(corr):
            continue
        results.append({"ticker": sym.ticker, "correlation": round(float(corr), 2)})

    results.sort(key=lambda item: item["correlation"], reverse=True)
    return results[:_MAX_CORRELATIONS_IN_PROMPT]


# --------------------------------------------------------------------------- #
# Phase 1: mark RUNNING + capture the (session-independent) run context
# --------------------------------------------------------------------------- #


def _prepare_run(symbol_id: int, trigger: str, run_id: int | None) -> dict:
    """Transition the run to RUNNING and snapshot everything the pipeline needs.

    Returns a dict with ``run_id`` and ``fatal`` (True when the symbol does not
    exist) plus, when not fatal, the captured context values.
    """
    with session_scope() as db:
        symbol = db.get(Symbol, symbol_id)
        if symbol is None:
            if run_id is not None:
                run = db.get(AnalysisRun, run_id)
                if run is not None:
                    run.status = RunStatus.FAILED.value
                    run.error = "Simbolo non trovato."
                    run.finished_at = datetime.utcnow()
            return {"run_id": run_id or 0, "fatal": True}

        run = db.get(AnalysisRun, run_id) if run_id is not None else None
        if run is None:
            run = AnalysisRun(
                symbol_id=symbol_id,
                status=RunStatus.RUNNING.value,
                trigger=trigger,
                started_at=datetime.utcnow(),
            )
            db.add(run)
            db.flush()
        else:
            run.status = RunStatus.RUNNING.value
            run.trigger = trigger or run.trigger
            run.started_at = datetime.utcnow()
            run.finished_at = None
            run.error = None
        run_id = run.id

        symbol_out = SymbolOut.model_validate(symbol)
        is_favorite = bool(symbol.is_favorite)
        ticker = symbol.ticker

        settings = db.get(AppSettings, 1)
        if settings is not None:
            risk_profile = settings.risk_profile
            total_budget = float(settings.total_budget)
            currency = settings.budget_currency
        else:
            risk_profile, total_budget, currency = "prudente", 10000.0, "EUR"

        prev = db.execute(
            select(Recommendation)
            .where(Recommendation.symbol_id == symbol_id)
            .order_by(Recommendation.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        previous_recommendation = (
            {"action": prev.action, "created_at": prev.created_at.isoformat()}
            if prev is not None
            else None
        )

    return {
        "run_id": run_id,
        "fatal": False,
        "symbol_out": symbol_out,
        "is_favorite": is_favorite,
        "ticker": ticker,
        "risk_profile": risk_profile,
        "total_budget": total_budget,
        "currency": currency,
        "previous_recommendation": previous_recommendation,
    }


# --------------------------------------------------------------------------- #
# Phase 2: gather market data (blocking; runs inside asyncio.to_thread)
# --------------------------------------------------------------------------- #


def _gather_market_data(symbol_id: int, ticker: str) -> dict:
    """Refresh + read all market data for one symbol (synchronous, own session)."""
    market = market_data_service
    news = news_service

    with session_scope() as db:
        symbol = db.get(Symbol, symbol_id)
        if symbol is not None:
            try:
                market.refresh_prices(db, symbol, interval="1d", days=730)
            except Exception:
                logger.warning("Refresh prezzi 1d fallito per %s", ticker, exc_info=True)
            try:
                market.refresh_prices(db, symbol, interval="1h", days=30)
            except Exception:
                logger.warning("Refresh prezzi 1h fallito per %s", ticker, exc_info=True)

        df_daily = market.get_history_df(db, symbol_id, interval="1d", days=730)

        try:
            correlations = _open_position_correlations(db, symbol_id, df_daily)
        except Exception:
            logger.warning("Calcolo correlazioni posizioni aperte fallito", exc_info=True)
            correlations = []

        try:
            macro_news = news.fetch_macro(db)
        except Exception:
            logger.warning("Fetch news macro fallito", exc_info=True)
            macro_news = []
        try:
            corporate_news = news.fetch_corporate(db, ticker)
        except Exception:
            logger.warning("Fetch news corporate fallito per %s", ticker, exc_info=True)
            corporate_news = []

    indicators_full = compute_all(df_daily)
    latest = indicators_full.get("latest", {}) if isinstance(indicators_full, dict) else {}
    price_summary = market.price_summary(df_daily)
    fundamentals = market.get_fundamentals(ticker)
    # Analyst forward estimates + revisions (own fetch, cached 6h). Attached under
    # the fundamentals payload as an optional "estimates" block; kept None when the
    # snapshot is fully empty (common for non-US tickers) so the fundamentals agent
    # simply sees no signal there. This must NOT affect the deterministic
    # skip logic, which looks only at the core numeric fields.
    estimates = market.get_analyst_estimates(ticker)
    fundamentals["estimates"] = (
        estimates if any(value is not None for value in estimates.values()) else None
    )
    # Same synchronous/blocking pattern as the fundamentals/market calls above
    # (this whole function already runs inside asyncio.to_thread at its call site).
    sentiment = market.get_sentiment_snapshot(ticker)
    # Global market-regime context (VIX, rates, FX, commodities, credit spreads).
    # Symbol-independent and behind its own 6h cache, so this single call serves
    # every symbol; deliberately kept OUT of the deterministic skip logic — it is
    # always-available context (surfaced to the validator via risk_metrics), never
    # itself a reason to run an otherwise-skipped agent.
    market_regime = market.get_market_regime()
    relative_performance = market.get_relative_performance(
        ticker,
        price_summary.get("change_pct_30d"),
        price_summary.get("change_pct_90d"),
        sector=fundamentals.get("sector"),
    )
    calendar = {
        "next_earnings_date": sentiment.get("next_earnings_date"),
        "days_to_earnings": sentiment.get("days_to_earnings"),
    }

    # Agent-facing indicators: scalar latest values + a compact recent-close series
    # capped at the most recent 20 closes (keeps prompt cost bounded).
    indicators_ctx = dict(latest)
    if isinstance(df_daily, pd.DataFrame) and not df_daily.empty and "close" in df_daily.columns:
        indicators_ctx["recent_closes"] = [
            round(float(c), 4) for c in df_daily["close"].tail(_MAX_RECENT_CLOSES).tolist()
        ]
    else:
        indicators_ctx["recent_closes"] = []

    metrics = _build_metrics(price_summary, latest)
    risk_metrics = _build_risk_metrics(metrics, fundamentals, sentiment, market_regime, correlations)

    return {
        "price_summary": price_summary,
        "indicators": indicators_ctx,
        "fundamentals": fundamentals,
        # News is capped/truncated for the prompts; persisted news_items are intact.
        "macro_news": _slim_news(macro_news),
        "corporate_news": _slim_news(corporate_news),
        "sentiment": sentiment,
        "relative_performance": relative_performance,
        "calendar": calendar,
        "market_regime": market_regime,
        "metrics": metrics,
        "risk_metrics": risk_metrics,
    }


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


async def run_analysis(
    symbol_id: int, trigger: str = "SCHEDULED", run_id: int | None = None
) -> int:
    """Run the full analysis pipeline for one symbol; return the run id.

    Never raises: any failure marks the run FAILED (with an Italian error
    message persisted for the UI) and is logged.
    """
    lock = _get_lock(symbol_id)
    async with lock:
        return await _run_analysis_locked(symbol_id, trigger, run_id)


async def _run_analysis_locked(symbol_id: int, trigger: str, run_id: int | None) -> int:
    # Phase 1 (must succeed to have a run row to report against).
    try:
        prep = _prepare_run(symbol_id, trigger, run_id)
    except Exception as exc:
        logger.exception("Inizializzazione run fallita per il simbolo %s", symbol_id)
        _safe_fail(run_id, f"Errore di inizializzazione dell'analisi: {exc}")
        return run_id or 0

    run_id = prep["run_id"]
    if prep.get("fatal"):
        return run_id

    try:
        # Phase 2: market data (blocking work off the event loop).
        data = await asyncio.to_thread(_gather_market_data, symbol_id, prep["ticker"])

        llm = _get_llm()

        # Phase 3: fan out over the analysts (each with its own lessons).
        def make_ctx(lessons: list[str]) -> AgentContext:
            return AgentContext(
                symbol=prep["symbol_out"],
                is_favorite=prep["is_favorite"],
                price_summary=data["price_summary"],
                indicators=data["indicators"],
                fundamentals=data["fundamentals"],
                macro_news=data["macro_news"],
                corporate_news=data["corporate_news"],
                risk_profile=prep["risk_profile"],
                lessons=lessons,
                sentiment=data["sentiment"],
                relative_performance=data["relative_performance"],
                calendar=data["calendar"],
                market_regime=data["market_regime"],
            )

        # Deterministic skips: don't pay for an LLM call when an analyst has
        # nothing to analyse. Skipped analysts get a deterministic OK output that
        # flows into the synthesizer exactly like a real one.
        deterministic = _deterministic_analyst_outputs(data)
        agents_to_run = [agent for agent in ANALYST_AGENTS if agent.name not in deterministic]
        run_results = await asyncio.gather(
            *(agent.run(make_ctx(_load_lessons(agent.name)), llm) for agent in agents_to_run),
            return_exceptions=True,
        )
        results_by_name: dict[str, Any] = {
            agent.name: result for agent, result in zip(agents_to_run, run_results)
        }
        for name, det_output in deterministic.items():
            results_by_name[name] = AgentResult(
                agent_name=name, output=det_output, provider=_DETERMINISTIC_PROVIDER
            )

        # Phase 4: persist every analyst (incl. FAILED), build the outputs map.
        # Deterministic skips are AgentResults, so they persist as OK and are not
        # counted toward the failure-abort threshold.
        analyst_outputs: dict[str, dict | None] = {}
        failed = 0
        with session_scope() as db:
            for agent in ANALYST_AGENTS:
                name = agent.name
                result = results_by_name[name]
                if isinstance(result, BaseException):
                    failed += 1
                    analyst_outputs[name] = None
                    db.add(
                        Analysis(
                            run_id=run_id,
                            symbol_id=symbol_id,
                            agent_name=name,
                            status="FAILED",
                            signal=None,
                            confidence=None,
                            stance=None,
                            summary_it="",
                            output_json="{}",
                            error=_err_text(result),
                        )
                    )
                else:
                    out = result.output
                    analyst_outputs[name] = out
                    db.add(
                        Analysis(
                            run_id=run_id,
                            symbol_id=symbol_id,
                            agent_name=name,
                            status="OK",
                            signal=out.get("signal"),
                            confidence=out.get("confidence"),
                            stance=out.get("stance"),
                            summary_it=out.get("summary_it", "") or "",
                            output_json=json.dumps(out, ensure_ascii=False),
                            error=None,
                        )
                    )

        if failed >= _MIN_FAILED_ANALYSTS_TO_ABORT:
            rate_limited = any(
                isinstance(result, BaseException) and "429" in _err_text(result)
                for result in results_by_name.values()
            )
            if rate_limited:
                message = (
                    "Analisi interrotta: raggiunto il limite di richieste del provider "
                    "LLM (HTTP 429). Attendi qualche minuto e riprova, oppure scegli un "
                    "modello o un piano con limiti più alti dalle Impostazioni."
                )
            else:
                message = (
                    f"Analisi interrotta: {failed} analisti su {len(ANALYST_AGENTS)} hanno fallito."
                )
            _safe_fail(run_id, message)
            return run_id

        # Slimmed copy for the actors' prompts (drops UI-only summary_it); the
        # persisted Analysis rows above keep the full outputs.
        analyst_outputs_slim = _slim_analyst_outputs(analyst_outputs)

        # Phase 5a: synthesizer.
        synthesizer = SynthesizerAgent()
        try:
            syn_result = await synthesizer.run(
                analyst_outputs=analyst_outputs_slim,
                price_summary=data["price_summary"],
                risk_profile=prep["risk_profile"],
                total_budget=prep["total_budget"],
                currency=prep["currency"],
                previous_recommendation=prep["previous_recommendation"],
                lessons=_load_lessons("synthesizer"),
                llm=llm,
            )
        except Exception as exc:
            logger.exception("Sintetizzatore fallito per la run %s", run_id)
            with session_scope() as db:
                db.add(
                    Analysis(
                        run_id=run_id,
                        symbol_id=symbol_id,
                        agent_name="synthesizer",
                        status="FAILED",
                        summary_it="",
                        output_json="{}",
                        error=_err_text(exc),
                    )
                )
            _safe_fail(run_id, f"Sintetizzatore non disponibile: {exc}")
            return run_id

        proposal = syn_result.output
        synth_provider = syn_result.provider
        with session_scope() as db:
            db.add(
                Analysis(
                    run_id=run_id,
                    symbol_id=symbol_id,
                    agent_name="synthesizer",
                    status="OK",
                    signal=_action_signal(proposal.get("action"), proposal.get("confidence")),
                    confidence=proposal.get("confidence"),
                    stance=None,
                    summary_it=proposal.get("rationale_it", "") or "",
                    output_json=json.dumps(proposal, ensure_ascii=False),
                    error=None,
                )
            )

        # Phase 5b: adversarial validator.
        validator = RiskValidatorAgent()
        try:
            val_result = await validator.run(
                proposal=proposal,
                analyst_outputs=analyst_outputs_slim,
                risk_metrics=data["risk_metrics"],
                lessons=_load_lessons("validator"),
                llm=llm,
            )
        except Exception as exc:
            logger.exception("Validatore fallito per la run %s", run_id)
            with session_scope() as db:
                db.add(
                    Analysis(
                        run_id=run_id,
                        symbol_id=symbol_id,
                        agent_name="validator",
                        status="FAILED",
                        signal=None,
                        summary_it="",
                        output_json="{}",
                        error=_err_text(exc),
                    )
                )
            _safe_fail(run_id, f"Validatore di rischio non disponibile: {exc}")
            return run_id

        verdict = val_result.output
        with session_scope() as db:
            db.add(
                Analysis(
                    run_id=run_id,
                    symbol_id=symbol_id,
                    agent_name="validator",
                    status="OK",
                    signal=None,
                    confidence=None,
                    stance=None,
                    summary_it=verdict.get("notes_it", "") or "",
                    output_json=json.dumps(verdict, ensure_ascii=False),
                    error=None,
                )
            )

        # Phase 6-7: deterministic policy + persist recommendation, complete run.
        metrics = data["metrics"]
        with session_scope() as db:
            settings = db.get(AppSettings, 1)
            if settings is None:
                settings = SimpleNamespace(
                    risk_profile=prep["risk_profile"],
                    max_position_pct=15.0,
                    cash_reserve_pct=30.0,
                    total_budget=prep["total_budget"],
                )
            last_reco = db.execute(
                select(Recommendation)
                .where(Recommendation.symbol_id == symbol_id)
                .order_by(Recommendation.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            open_allocation_pct = _compute_open_allocation(db, symbol_id)

            final, checks = PolicyEngine.apply(
                proposal, verdict, metrics, settings, last_reco, open_allocation_pct
            )

            db.add(
                Recommendation(
                    run_id=run_id,
                    symbol_id=symbol_id,
                    action=final.action,
                    sizing_strategy=final.sizing_strategy,
                    confidence=final.confidence,
                    allocation_pct=final.allocation_pct,
                    allocation_amount=final.allocation_amount,
                    dca_tranches=final.dca_tranches,
                    entry_price=final.entry_price,
                    stop_loss_price=final.stop_loss_price,
                    take_profit_price=final.take_profit_price,
                    horizon_days=final.horizon_days,
                    estimated_profit_pct=final.estimated_profit_pct,
                    estimated_profit_amount=final.estimated_profit_amount,
                    rationale_it=proposal.get("rationale_it", "") or "",
                    synthesizer_json=json.dumps(proposal, ensure_ascii=False),
                    validator_verdict=verdict.get("verdict", "APPROVE"),
                    validator_notes_it=verdict.get("notes_it", "") or "",
                    policy_checks_json=json.dumps(
                        [c.model_dump() for c in checks], ensure_ascii=False
                    ),
                    policy_overridden=final.policy_overridden,
                    original_action=final.original_action,
                    original_sizing=final.original_sizing,
                )
            )
            run = db.get(AnalysisRun, run_id)
            if run is not None:
                run.status = RunStatus.COMPLETED.value
                run.finished_at = datetime.utcnow()
                run.llm_provider_used = synth_provider

        return run_id

    except Exception as exc:
        logger.exception("Run di analisi %s fallita", run_id)
        _safe_fail(run_id, f"Errore imprevisto durante l'analisi: {exc}")
        return run_id
