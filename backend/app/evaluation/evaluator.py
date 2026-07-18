"""Weekly evaluation engine (blueprint section 7.1).

``run_weekly_evaluation()`` scores every recommendation that is now at least
seven days old against realized market outcomes, attributes accuracy to each of
the six pipeline actors, aggregates portfolio-level metrics, and always persists
an :class:`~app.models.Evaluation` row (even when there is nothing to score).
It then hands off to the LLM-backed feedback loop
(:mod:`app.evaluation.feedback`) to write per-agent lessons and an Italian
report, wrapping both so an LLM outage can never fail the evaluation itself.

Concurrency: a module-level :class:`asyncio.Lock` prevents two evaluations from
running at once. If one is already in flight, callers get a ``RuntimeError``
(the Sunday cron job swallows it; the manual endpoint maps it to HTTP 409).

Every datetime handled here is UTC-naive, matching the DB convention.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select

from app.data.market import market_data_service
from app.db import session_scope
from app.evaluation import feedback
from app.models import Analysis, Evaluation, PriceHistory, Recommendation, Symbol

logger = logging.getLogger(__name__)

# The four analysts plus the two decision actors. Order is stable so the
# per-agent JSON always lists every agent, even with zero samples.
ANALYST_AGENTS: tuple[str, ...] = (
    "technical",
    "fundamentals",
    "macro_news",
    "corporate_news",
)
AGENT_NAMES: tuple[str, ...] = ANALYST_AGENTS + ("synthesizer", "validator")

EVALUATION_WINDOW_DAYS = 7
# outcome_score above this counts the recommendation (or final action) as correct.
CORRECT_THRESHOLD = 0.2
# |signal| below this is treated as a "flat"/no-direction prediction.
FLAT_SIGNAL_THRESHOLD = 0.15
# A flat prediction is correct when the realized move stayed within this band (%).
FLAT_RETURN_BAND_PCT = 2.0
# Default confidence weight when an analysis carries no confidence value.
DEFAULT_CONFIDENCE_WEIGHT = 0.5

# Serialized while the evaluation runs; a second concurrent call raises.
_evaluation_lock = asyncio.Lock()


@dataclass
class _EvalItem:
    """A single recommendation successfully scored against realized prices."""

    rec: Recommendation
    ret: float          # realized_return_7d, in percent
    score: float        # outcome_score, -1..+1
    correct: bool       # outcome_score > CORRECT_THRESHOLD


# --------------------------------------------------------------------------- #
# Pure scoring helpers (blueprint 7.1 formulas)
# --------------------------------------------------------------------------- #


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _outcome_score(action: str, ret: float) -> float:
    """Score an action against a realized 7d return (%), yielding -1..+1.

    * BUY  -> clamp(ret / 5, -1, 1)
    * SELL -> clamp(-ret / 5, -1, 1)
    * HOLD -> 1 - min(|ret| / 5, 1) * 2   (rewards a flat market)
    """
    if action == "BUY":
        return _clamp(ret / 5.0)
    if action == "SELL":
        return _clamp(-ret / 5.0)
    # HOLD (and any unexpected action defaults to the conservative HOLD score).
    return 1.0 - min(abs(ret) / 5.0, 1.0) * 2.0


def _ideal_signal(ret: float) -> float:
    """The signal a perfect analyst would have emitted for this realized move."""
    return _clamp(ret / 5.0)


def _confidence_weight(confidence: float | None) -> float:
    if confidence is None:
        return DEFAULT_CONFIDENCE_WEIGHT
    return float(confidence)


def _analyst_sample(signal: float, confidence: float | None, ret: float) -> tuple[bool, float, float]:
    """(correct, weight, signal_error) for one analyst prediction."""
    if abs(signal) < FLAT_SIGNAL_THRESHOLD:
        correct = abs(ret) < FLAT_RETURN_BAND_PCT
    elif ret == 0:
        correct = False
    else:
        correct = (signal > 0) == (ret > 0)
    signal_error = abs(signal - _ideal_signal(ret))
    return correct, _confidence_weight(confidence), signal_error


def _aggregate(samples: list[tuple[bool, float, float | None]]) -> dict:
    """Confidence-weighted accuracy + avg signal error for one agent."""
    n = len(samples)
    if n == 0:
        return {"accuracy": None, "avg_signal_error": None, "n_samples": 0}

    weight_total = sum(weight for _, weight, _ in samples)
    if weight_total > 0:
        accuracy = sum(weight for correct, weight, _ in samples if correct) / weight_total
    else:  # every weight was zero -> fall back to a plain mean
        accuracy = sum(1 for correct, _, _ in samples if correct) / n

    errs = [(weight, err) for _, weight, err in samples if err is not None]
    if errs:
        err_weight_total = sum(weight for weight, _ in errs)
        if err_weight_total > 0:
            avg_err: float | None = sum(weight * err for weight, err in errs) / err_weight_total
        else:
            avg_err = sum(err for _, err in errs) / len(errs)
    else:
        avg_err = None

    return {
        "accuracy": round(accuracy, 4),
        "avg_signal_error": round(avg_err, 4) if avg_err is not None else None,
        "n_samples": n,
    }


# --------------------------------------------------------------------------- #
# Price lookups (blueprint 7.1 step 2)
# --------------------------------------------------------------------------- #


def _first_close_at_or_after(db, symbol_id: int, target: datetime) -> float | None:
    """First 1d close with ``ts >= target`` for a symbol, or None if absent."""
    row = db.execute(
        select(PriceHistory.close)
        .where(
            PriceHistory.symbol_id == symbol_id,
            PriceHistory.interval == "1d",
            PriceHistory.ts >= target,
        )
        .order_by(PriceHistory.ts.asc())
        .limit(1)
    ).scalar_one_or_none()
    return float(row) if row is not None else None


async def _realized_return(db, rec: Recommendation, symbols_cache: dict[int, Symbol | None]) -> float | None:
    """Realized 7d return (%) for a recommendation, refreshing prices if needed.

    Returns ``None`` when the price at ``created_at + 7d`` cannot be resolved
    even after a yfinance refresh — the caller then leaves the rec unevaluated.
    Assumes the caller has already guarded ``entry_price`` (> 0).
    """
    entry = rec.entry_price
    if entry is None or entry <= 0:
        return None

    target = rec.created_at + timedelta(days=EVALUATION_WINDOW_DAYS)
    close = _first_close_at_or_after(db, rec.symbol_id, target)

    if close is None:
        symbol = symbols_cache.get(rec.symbol_id)
        if rec.symbol_id not in symbols_cache:
            symbol = db.get(Symbol, rec.symbol_id)
            symbols_cache[rec.symbol_id] = symbol
        if symbol is not None:
            try:
                # yfinance is blocking; refresh using this session so the
                # committed rows are visible to the very next read below.
                await asyncio.to_thread(
                    market_data_service.refresh_prices, db, symbol, "1d", 730
                )
            except Exception:
                logger.exception("Price refresh failed for %s", symbol.ticker)
            close = _first_close_at_or_after(db, rec.symbol_id, target)

    if close is None or close <= 0:
        return None
    return (close - entry) / entry * 100.0


# --------------------------------------------------------------------------- #
# Per-agent attribution (blueprint 7.1 step 4)
# --------------------------------------------------------------------------- #


def _attribute_per_agent(db, items: list[_EvalItem]) -> dict:
    """Build ``{agent: {accuracy, avg_signal_error, n_samples}}`` for all 6 agents."""
    samples: dict[str, list[tuple[bool, float, float | None]]] = {
        name: [] for name in AGENT_NAMES
    }

    run_ids = [item.rec.run_id for item in items]
    analyses_by_run: dict[int, dict[str, Analysis]] = {}
    if run_ids:
        rows = (
            db.execute(select(Analysis).where(Analysis.run_id.in_(run_ids)))
            .scalars()
            .all()
        )
        for analysis in rows:
            analyses_by_run.setdefault(analysis.run_id, {})[analysis.agent_name] = analysis

    for item in items:
        rec = item.rec
        ret = item.ret
        by_agent = analyses_by_run.get(rec.run_id, {})

        # --- four analysts: directional accuracy + signal error ---
        for agent in ANALYST_AGENTS:
            analysis = by_agent.get(agent)
            if analysis is None or analysis.status != "OK" or analysis.signal is None:
                continue
            samples[agent].append(_analyst_sample(analysis.signal, analysis.confidence, ret))

        # --- synthesizer: scored on the final action correctness ---
        synth = by_agent.get("synthesizer")
        synth_weight = _confidence_weight(
            synth.confidence if synth is not None else rec.confidence
        )
        synth_err = (
            abs(synth.signal - _ideal_signal(ret))
            if synth is not None and synth.signal is not None
            else None
        )
        samples["synthesizer"].append((item.correct, synth_weight, synth_err))

        # --- validator: final action, plus useful-veto credit ---
        validator = by_agent.get("validator")
        validator_weight = _confidence_weight(
            validator.confidence if validator is not None else rec.confidence
        )
        if rec.validator_verdict == "VETO":
            # The proposal was blocked; original_action is what would have run.
            would_have = rec.original_action or rec.action
            veto_useful = _outcome_score(would_have, ret) <= 0
            samples["validator"].append((veto_useful, validator_weight, None))
        else:
            samples["validator"].append((item.correct, validator_weight, None))

    return {name: _aggregate(samples[name]) for name in AGENT_NAMES}


# --------------------------------------------------------------------------- #
# Portfolio-level aggregates (blueprint 7.1 steps 3, 5, 6)
# --------------------------------------------------------------------------- #


def _portfolio_metrics(
    items: list[_EvalItem], symbols_cache: dict[int, Symbol | None]
) -> dict:
    if not items:
        return {
            "accuracy_overall": None,
            "avg_realized_return_pct": None,
            "hypothetical_pnl_pct": None,
            "best_symbol": None,
            "worst_symbol": None,
        }

    accuracy_overall = sum(1 for item in items if item.correct) / len(items)
    avg_realized_return = sum(item.ret for item in items) / len(items)

    buys = [item for item in items if item.rec.action == "BUY"]
    if buys:
        hypothetical_pnl = sum(
            item.ret * (item.rec.allocation_pct or 0.0) / 100.0 for item in buys
        ) / len(buys)
    else:
        hypothetical_pnl = None

    # mean outcome_score per symbol -> best / worst ticker
    per_symbol_scores: dict[int, list[float]] = {}
    for item in items:
        per_symbol_scores.setdefault(item.rec.symbol_id, []).append(item.score)
    means = {
        symbol_id: sum(scores) / len(scores)
        for symbol_id, scores in per_symbol_scores.items()
    }

    def _ticker(symbol_id: int) -> str | None:
        symbol = symbols_cache.get(symbol_id)
        return symbol.ticker if symbol is not None else None

    best_id = max(means, key=means.get)
    worst_id = min(means, key=means.get)

    return {
        "accuracy_overall": round(accuracy_overall, 4),
        "avg_realized_return_pct": round(avg_realized_return, 4),
        "hypothetical_pnl_pct": round(hypothetical_pnl, 4) if hypothetical_pnl is not None else None,
        "best_symbol": _ticker(best_id),
        "worst_symbol": _ticker(worst_id),
    }


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


async def run_weekly_evaluation() -> Evaluation:
    """Score due recommendations, persist an ``Evaluation``, trigger feedback.

    Always returns a persisted :class:`~app.models.Evaluation` (with zero counts
    when nothing was due). Raises ``RuntimeError`` if another evaluation is
    already running.
    """
    if _evaluation_lock.locked():
        raise RuntimeError("Valutazione già in corso")
    async with _evaluation_lock:
        return await _run_weekly_evaluation_impl()


async def _run_weekly_evaluation_impl() -> Evaluation:
    now = datetime.utcnow()
    cutoff = now - timedelta(days=EVALUATION_WINDOW_DAYS)
    symbols_cache: dict[int, Symbol | None] = {}

    with session_scope() as db:
        candidates = (
            db.execute(
                select(Recommendation)
                .where(
                    Recommendation.evaluated.is_(False),
                    Recommendation.created_at <= cutoff,
                )
                .order_by(Recommendation.created_at.asc())
            )
            .scalars()
            .all()
        )

        items: list[_EvalItem] = []
        for rec in candidates:
            if rec.entry_price is None or rec.entry_price <= 0:
                continue  # cannot compute a return; leave unevaluated
            ret = await _realized_return(db, rec, symbols_cache)
            if ret is None:
                continue  # prices still missing after refresh; leave unevaluated
            score = _outcome_score(rec.action, ret)
            items.append(
                _EvalItem(rec=rec, ret=ret, score=score, correct=score > CORRECT_THRESHOLD)
            )

        # Make sure every evaluated symbol is cached (for best/worst tickers).
        for item in items:
            if item.rec.symbol_id not in symbols_cache:
                symbols_cache[item.rec.symbol_id] = db.get(Symbol, item.rec.symbol_id)

        per_agent = _attribute_per_agent(db, items)
        portfolio = _portfolio_metrics(items, symbols_cache)

        period_start = min((item.rec.created_at for item in items), default=cutoff)

        evaluation = Evaluation(
            period_start=period_start,
            period_end=now,
            status="COMPLETED",
            total_recommendations=len(candidates),
            evaluated_count=len(items),
            accuracy_overall=portfolio["accuracy_overall"],
            avg_realized_return_pct=portfolio["avg_realized_return_pct"],
            hypothetical_pnl_pct=portfolio["hypothetical_pnl_pct"],
            best_symbol=portfolio["best_symbol"],
            worst_symbol=portfolio["worst_symbol"],
            per_agent_json=json.dumps(per_agent),
            report_it="",
        )
        db.add(evaluation)
        db.flush()  # assign evaluation.id before wiring recommendations to it
        evaluation_id = evaluation.id

        for item in items:
            item.rec.evaluated = True
            item.rec.realized_return_7d = item.ret
            item.rec.outcome_score = item.score
            item.rec.evaluation_id = evaluation_id
        # session_scope commits on exit

    logger.info(
        "Weekly evaluation %s: %d/%d recommendations scored",
        evaluation_id,
        len(items),
        len(candidates),
    )

    # LLM-backed feedback: never allowed to fail the evaluation.
    try:
        await feedback.generate_lessons(evaluation_id)
    except Exception:
        logger.exception("generate_lessons failed for evaluation %s", evaluation_id)
    try:
        await feedback.generate_report_it(evaluation_id)
    except Exception:
        logger.exception("generate_report_it failed for evaluation %s", evaluation_id)

    with session_scope() as db:
        return db.get(Evaluation, evaluation_id)


# --------------------------------------------------------------------------- #
# Trends (used by the API to fill AgentMetrics.trend)
# --------------------------------------------------------------------------- #


def get_agent_trends(db, last_n: int = 8) -> dict[str, list[float]]:
    """Per-agent accuracy series across the most recent evaluations.

    Reads ``per_agent_json`` from the ``last_n`` newest evaluations and returns
    ``{agent_name: [accuracy_oldest, ..., accuracy_newest]}``, skipping any
    evaluation where a given agent has no accuracy value.
    """
    rows = (
        db.execute(
            select(Evaluation.per_agent_json)
            .order_by(Evaluation.created_at.desc())
            .limit(last_n)
        )
        .scalars()
        .all()
    )

    trends: dict[str, list[float]] = {name: [] for name in AGENT_NAMES}
    for per_agent_json in reversed(rows):  # oldest -> newest
        try:
            per_agent = json.loads(per_agent_json) if per_agent_json else {}
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(per_agent, dict):
            continue
        for name in AGENT_NAMES:
            metrics = per_agent.get(name)
            if not isinstance(metrics, dict):
                continue
            accuracy = metrics.get("accuracy")
            if accuracy is not None:
                trends.setdefault(name, []).append(float(accuracy))
    return trends
