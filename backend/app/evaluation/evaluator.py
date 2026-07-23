"""Weekly evaluation engine (blueprint section 7.1).

``run_weekly_evaluation()`` scores every recommendation that is now at least
seven days old against realized market outcomes, attributes accuracy to each of
the seven pipeline actors, aggregates portfolio-level metrics, and always persists
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
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select

from app.data.market import market_data_service
from app.db import session_scope
from app.evaluation import feedback
from app.evaluation.features import compute_feature_stats
from app.models import Analysis, Evaluation, PriceHistory, Recommendation, Symbol

logger = logging.getLogger(__name__)

# The five analysts plus the two decision actors. Order is stable so the
# per-agent JSON always lists every agent, even with zero samples.
ANALYST_AGENTS: tuple[str, ...] = (
    "technical",
    "fundamentals",
    "macro_news",
    "corporate_news",
    "sentiment",
)
AGENT_NAMES: tuple[str, ...] = ANALYST_AGENTS + ("synthesizer", "validator")

EVALUATION_WINDOW_DAYS = 7
# outcome_score above this counts the recommendation (or final action) as correct.
CORRECT_THRESHOLD = 0.2
# |signal| below this is treated as a "flat"/no-direction prediction.
FLAT_SIGNAL_THRESHOLD = 0.15
# A flat prediction is correct when the realized move stayed within this band (%),
# scaled by the same norm as the outcome score (see _analyst_sample).
FLAT_RETURN_BAND_PCT = 2.0
# Default confidence weight when an analysis carries no confidence value.
DEFAULT_CONFIDENCE_WEIGHT = 0.5
# Fixed 7-day-equivalent normalization: at the base 7-day window, a +-5% move
# saturates the outcome score to +-1.0. _norm_pct scales this for other windows.
BASE_NORM_PCT = 5.0

# --------------------------------------------------------------------------- #
# Horizon-aware evaluation (blueprint §7 addendum, part 2 of 4)
# --------------------------------------------------------------------------- #
# A second, independent checkpoint that scores a recommendation once IT reaches
# its own stated horizon_days (clamped to a sane range) instead of the fixed
# 7-day window above, and — for BUY/SELL, when a benchmark return is
# resolvable — relative to the benchmark rather than in absolute terms. This
# never replaces the 7-day checkpoint (kept byte-for-byte unchanged above,
# including for existing tests): the 7-day pass gives fast weekly feedback,
# the horizon pass judges what the recommendation actually claimed.
HORIZON_MIN_DAYS = 7
HORIZON_MAX_DAYS = 60

# Serialized while the evaluation runs; a second concurrent call raises.
_evaluation_lock = asyncio.Lock()


@dataclass
class _EvalItem:
    """A single recommendation successfully scored against realized prices.

    Generic over BOTH evaluation passes: the 7-day checkpoint constructs these
    with the default ``norm`` (5.0, i.e. the original fixed-window behavior,
    unchanged); the horizon pass sets ``norm`` to the window-scaled value from
    :func:`_norm_pct` and ``ret`` to the effective (possibly excess-of-benchmark)
    return that was actually scored. ``_attribute_per_agent`` is reused verbatim
    for both.
    """

    rec: Recommendation
    ret: float          # the return actually scored against, in percent
    score: float         # outcome_score, -1..+1
    correct: bool        # score > CORRECT_THRESHOLD
    norm: float = BASE_NORM_PCT   # +-norm% saturates the score to +-1.0


# --------------------------------------------------------------------------- #
# Pure scoring helpers (blueprint 7.1 formulas)
# --------------------------------------------------------------------------- #


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _outcome_score(action: str, ret: float, norm: float = BASE_NORM_PCT) -> float:
    """Score an action against a realized return (%), yielding -1..+1.

    * BUY  -> clamp(ret / norm, -1, 1)
    * SELL -> clamp(-ret / norm, -1, 1)
    * HOLD -> 1 - min(|ret| / norm, 1) * 2   (rewards a flat market)

    ``norm`` defaults to the original fixed 7-day scale (5.0): a +-5% move
    saturates the score. The horizon pass instead passes a window-scaled norm
    from :func:`_norm_pct` so a longer-horizon recommendation isn't judged
    against the same tight band as a 7-day one.
    """
    if action == "BUY":
        return _clamp(ret / norm)
    if action == "SELL":
        return _clamp(-ret / norm)
    # HOLD (and any unexpected action defaults to the conservative HOLD score).
    return 1.0 - min(abs(ret) / norm, 1.0) * 2.0


def _ideal_signal(ret: float, norm: float = BASE_NORM_PCT) -> float:
    """The signal a perfect analyst would have emitted for this realized move."""
    return _clamp(ret / norm)


def _norm_pct(window_days: int) -> float:
    """Window-scaled score normalization, sqrt(t)-scaled from the 7-day base.

    Realized-return volatility scales roughly with sqrt(time), so the +-5%
    saturation band calibrated for 7 days becomes +-5*sqrt(window/7)% for a
    longer (or shorter) window — e.g. ~9.3% at the 30-day default horizon,
    ~12.2% at the 60-day cap. Pure and total: any positive ``window_days`` works.
    """
    return BASE_NORM_PCT * math.sqrt(window_days / EVALUATION_WINDOW_DAYS)


def _clamped_horizon(rec: Recommendation) -> int:
    """``rec.horizon_days`` clamped to [HORIZON_MIN_DAYS, HORIZON_MAX_DAYS].

    Guards against a missing/zero/unreasonable horizon on old or malformed
    rows; the synthesizer's own default is 30 days.
    """
    days = rec.horizon_days or 30
    return max(HORIZON_MIN_DAYS, min(HORIZON_MAX_DAYS, days))


def _resolve_effective_return(
    action: str, realized: float, benchmark: float | None
) -> tuple[float, str]:
    """(effective_ret, basis) used for the horizon outcome score.

    BUY/SELL are scored relative to the benchmark ("excess") when a benchmark
    return was resolvable; HOLD is always scored on the absolute return — a
    HOLD's job is capital preservation ("don't touch it"), not beating the
    market, so a HOLD that fell 3% while the market fell 10% is NOT penalized
    for "underperforming". Falls back to "absolute" for BUY/SELL too when no
    benchmark return could be resolved (never estimated).
    """
    if action in ("BUY", "SELL") and benchmark is not None:
        return realized - benchmark, "excess"
    return realized, "absolute"


def _confidence_weight(confidence: float | None) -> float:
    if confidence is None:
        return DEFAULT_CONFIDENCE_WEIGHT
    return float(confidence)


def _analyst_sample(
    signal: float, confidence: float | None, ret: float, norm: float = BASE_NORM_PCT
) -> tuple[bool, float, float]:
    """(correct, weight, signal_error) for one analyst prediction.

    ``norm`` scales both the flat-return band and the ideal-signal comparison
    to the same window as the recommendation being judged (see :func:`_norm_pct`);
    the default reproduces the original fixed 7-day behavior exactly.
    """
    flat_band = FLAT_RETURN_BAND_PCT * (norm / BASE_NORM_PCT)
    if abs(signal) < FLAT_SIGNAL_THRESHOLD:
        correct = abs(ret) < flat_band
    elif ret == 0:
        correct = False
    else:
        correct = (signal > 0) == (ret > 0)
    signal_error = abs(signal - _ideal_signal(ret, norm))
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


async def _realized_return(
    db,
    rec: Recommendation,
    symbols_cache: dict[int, Symbol | None],
    window_days: int = EVALUATION_WINDOW_DAYS,
) -> float | None:
    """Realized return (%) for a recommendation over ``window_days``, refreshing prices if needed.

    Returns ``None`` when the price at ``created_at + window_days`` cannot be
    resolved even after a yfinance refresh — the caller then leaves the rec
    unevaluated. Assumes the caller has already guarded ``entry_price`` (> 0).
    ``window_days`` defaults to the fixed 7-day checkpoint; the horizon pass
    calls this with each recommendation's own clamped horizon instead.
    """
    entry = rec.entry_price
    if entry is None or entry <= 0:
        return None

    target = rec.created_at + timedelta(days=window_days)
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


async def _benchmark_return_for(
    symbol: Symbol, start: datetime, end: datetime
) -> float | None:
    """Benchmark % return over [start, end] for ``symbol``'s exchange, or None.

    Wraps the blocking :meth:`MarketDataService.get_benchmark_window_return` in
    ``asyncio.to_thread``; never raises — a resolution failure (network, thin
    history) simply means the horizon score falls back to the absolute return
    (see :func:`_resolve_effective_return`), never an estimated one.
    """
    benchmark = market_data_service.benchmark_for(symbol.ticker)
    try:
        return await asyncio.to_thread(
            market_data_service.get_benchmark_window_return, benchmark, start, end
        )
    except Exception:
        logger.exception(
            "Benchmark window return failed for %s (%s)", symbol.ticker, benchmark
        )
        return None


# --------------------------------------------------------------------------- #
# Per-agent attribution (blueprint 7.1 step 4)
# --------------------------------------------------------------------------- #


def _attribute_per_agent(db, items: list[_EvalItem]) -> dict:
    """Build ``{agent: {accuracy, avg_signal_error, n_samples}}`` for all 7 agents.

    Generic over which ``_EvalItem`` queue is passed: each item carries its own
    ``ret``/``norm``, so this single implementation serves both the 7-day
    checkpoint (default norm) and the horizon pass (window-scaled norm) without
    duplicating the attribution logic.
    """
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

        # --- analysts: directional accuracy + signal error ---
        for agent in ANALYST_AGENTS:
            analysis = by_agent.get(agent)
            if analysis is None or analysis.status != "OK" or analysis.signal is None:
                continue
            samples[agent].append(
                _analyst_sample(analysis.signal, analysis.confidence, ret, item.norm)
            )

        # --- synthesizer: scored on the final action correctness ---
        synth = by_agent.get("synthesizer")
        synth_weight = _confidence_weight(
            synth.confidence if synth is not None else rec.confidence
        )
        synth_err = (
            abs(synth.signal - _ideal_signal(ret, item.norm))
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
            veto_useful = _outcome_score(would_have, ret, item.norm) <= 0
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

        # --- Horizon-aware pass: score once EACH recommendation reaches its own
        # (clamped) horizon_days, relative to the benchmark for BUY/SELL. Fully
        # independent of the 7-day pass above: a rec can be `evaluated` (7-day
        # done) while still awaiting its own, later horizon maturity.
        candidates_h = (
            db.execute(
                select(Recommendation)
                .where(
                    Recommendation.evaluated_h.is_(False),
                    Recommendation.created_at <= now - timedelta(days=HORIZON_MIN_DAYS),
                )
                .order_by(Recommendation.created_at.asc())
            )
            .scalars()
            .all()
        )

        items_h: list[_EvalItem] = []
        for rec in candidates_h:
            window = _clamped_horizon(rec)
            if rec.created_at + timedelta(days=window) > now:
                continue  # this rec's OWN horizon hasn't matured yet
            if rec.entry_price is None or rec.entry_price <= 0:
                continue  # cannot compute a return; leave unevaluated
            ret = await _realized_return(db, rec, symbols_cache, window_days=window)
            if ret is None:
                continue  # prices still missing after refresh; leave unevaluated

            if rec.symbol_id not in symbols_cache:
                symbols_cache[rec.symbol_id] = db.get(Symbol, rec.symbol_id)
            symbol = symbols_cache.get(rec.symbol_id)
            benchmark_ret = (
                await _benchmark_return_for(
                    symbol, rec.created_at, rec.created_at + timedelta(days=window)
                )
                if symbol is not None
                else None
            )
            excess_ret = (ret - benchmark_ret) if benchmark_ret is not None else None
            effective_ret, basis = _resolve_effective_return(rec.action, ret, benchmark_ret)
            norm = _norm_pct(window)
            score = _outcome_score(rec.action, effective_ret, norm)

            items_h.append(
                _EvalItem(
                    rec=rec,
                    ret=effective_ret,
                    score=score,
                    correct=score > CORRECT_THRESHOLD,
                    norm=norm,
                )
            )
            rec.evaluated_h = True
            rec.realized_return_h = ret
            rec.benchmark_return_h = benchmark_ret
            rec.excess_return_h = excess_ret
            rec.outcome_score_h = score
            rec.outcome_basis_h = basis

        per_agent_7d = _attribute_per_agent(db, items)
        per_agent_final = _attribute_per_agent(db, items_h)
        per_agent = {
            name: {**per_agent_7d[name], "final": per_agent_final[name]}
            for name in AGENT_NAMES
        }
        portfolio = _portfolio_metrics(items, symbols_cache)

        # Cumulative per-feature validation (blueprint §7 addendum, part 3 of 4):
        # never allowed to fail the evaluation itself, even though it's pure
        # Python with no I/O -- consistent with every other step here.
        try:
            feature_stats = compute_feature_stats(db)
        except Exception:
            logger.exception("compute_feature_stats failed")
            feature_stats = None

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
            feature_stats_json=(
                json.dumps(feature_stats, ensure_ascii=False)
                if feature_stats is not None
                else None
            ),
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
        # items_h already mutated their Recommendation rows above (evaluated_h,
        # realized_return_h, etc.); nothing further to assign here.
        # session_scope commits on exit

    logger.info(
        "Weekly evaluation %s: %d/%d recommendations scored (7d); %d scored at own horizon",
        evaluation_id,
        len(items),
        len(candidates),
        len(items_h),
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


def get_pending_status(db) -> dict:
    """Snapshot of not-yet-evaluated recommendations, for the Performance page.

    New recommendations only become scoreable once they are at least
    ``EVALUATION_WINDOW_DAYS`` old (the evaluator needs a realized 7-day return).
    Right after the app is set up — or after adding a new agent/symbol — this
    naturally means "0 valutati" for a while, which reads as broken to a
    non-technical user. This lets the UI say instead: "N consigli in attesa, M
    già maturi, il prossimo lotto sarà valutabile il ...".

    Returns ``{"pending_count", "ready_count", "next_evaluable_at"}``;
    ``next_evaluable_at`` is ``None`` when there is nothing pending.
    """
    now = datetime.utcnow()
    cutoff = now - timedelta(days=EVALUATION_WINDOW_DAYS)

    rows = (
        db.execute(
            select(Recommendation.created_at)
            .where(Recommendation.evaluated.is_(False))
            .order_by(Recommendation.created_at.asc())
        )
        .scalars()
        .all()
    )

    ready_count = sum(1 for created_at in rows if created_at <= cutoff)
    pending_count = len(rows) - ready_count
    next_evaluable_at = (
        min(created_at for created_at in rows if created_at > cutoff) + timedelta(days=EVALUATION_WINDOW_DAYS)
        if pending_count > 0
        else None
    )

    return {
        "pending_count": pending_count,
        "ready_count": ready_count,
        "next_evaluable_at": next_evaluable_at,
    }
