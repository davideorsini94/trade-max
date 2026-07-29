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
#: HOLD needs a HIGHER bar than a directional call. Its score is
#: ``1 - 2*min(|ret|/norm, 1)``, so the generic 0.2 threshold called a HOLD
#: "correct" for any |ret| < 2% at the 7-day window — i.e. almost every quiet
#: week, which handed a free ~92% accuracy to whatever forced the HOLD. At 0.6
#: the market must really have stayed put: |ret| < 0.2*norm (1% at 7 days), the
#: same 1% a BUY has to beat to be scored correct.
HOLD_CORRECT_THRESHOLD = 0.6
# |signal| below this is treated as a "flat"/no-direction prediction.
FLAT_SIGNAL_THRESHOLD = 0.15
# A flat prediction is correct when the realized move stayed within this band (%),
# scaled by the same norm as the outcome score (see _analyst_sample).
FLAT_RETURN_BAND_PCT = 2.0
# Default confidence weight when an analysis carries no confidence value.
DEFAULT_CONFIDENCE_WEIGHT = 0.5
#: Floors for regenerating per-agent lessons. Deliberately softer than the
#: statistical gate on the DISPLAYED accuracy (lessons are qualitative), but a
#: batch of one recommendation must not rewrite an agent's standing instructions.
LESSONS_MIN_ITEMS = 5
LESSONS_MIN_SYMBOLS = 3
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
    correct: bool        # see _is_correct (HOLD is held to a higher bar)
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


def _is_correct(action: str, score: float) -> bool:
    """Was this action right? HOLD is held to :data:`HOLD_CORRECT_THRESHOLD`.

    A directional call (BUY/SELL) only has to beat CORRECT_THRESHOLD, but "do
    nothing" must not be scored correct merely because the market was quiet —
    that made inaction the safest way to look accurate.
    """
    threshold = HOLD_CORRECT_THRESHOLD if action == "HOLD" else CORRECT_THRESHOLD
    return score > threshold


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


#: Daily bars are stored at midnight LOCAL EXCHANGE time converted to naive UTC
#: (04:00 for a US listing, 22:00 of the PRIOR calendar day for Milan), so the
#: same +12h shift used for cross-exchange alignment elsewhere (see
#: ``app.engine.orchestrator._daily_returns_by_session``) maps a raw ``ts`` to
#: its trading-session date. A bar belongs to session date D exactly when
#: ``(ts + 12h).date() == D``, which makes "session date >= D" equivalent to the
#: SQL-friendly ``ts >= midnight(D) - 12h``.
_SESSION_DATE_SHIFT = timedelta(hours=12)


def _session_ts_threshold(target: datetime) -> datetime:
    """Lowest ``ts`` still belonging to ``target``'s session date (or a later one)."""
    return datetime.combine(target.date(), datetime.min.time()) - _SESSION_DATE_SHIFT


def _first_close_at_or_after(db, symbol_id: int, target: datetime) -> float | None:
    """First 1d close on ``target``'s trading SESSION or a later one, else None.

    Compares session DATES, not raw timestamps. ``target`` keeps the
    recommendation's time of day (e.g. 17:09) while the daily bar for that very
    session is stored at exchange midnight (04:00 UTC for a US listing), so a
    naive ``ts >= target`` would reject the target session's own close and
    silently demand the NEXT one — scoring every recommendation a full session
    late, and making the "N consigli maturi" counter promise an evaluation the
    evaluator could not yet deliver.
    """
    row = db.execute(
        select(PriceHistory.close)
        .where(
            PriceHistory.symbol_id == symbol_id,
            PriceHistory.interval == "1d",
            PriceHistory.ts >= _session_ts_threshold(target),
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


def _proposed_action(rec: Recommendation) -> str:
    """The action the SYNTHESIZER proposed, before validator/policy touched it.

    Read from ``synthesizer_json`` (always persisted with the proposal) rather
    than ``original_action``: the latter is set by the policy engine on ANY
    override, so it cannot tell "the validator downgraded this" from "a policy
    rule did". Falls back to ``original_action``, then to the final action.
    """
    try:
        action = (json.loads(rec.synthesizer_json or "{}") or {}).get("action")
    except (TypeError, ValueError):
        action = None
    if action in ("BUY", "SELL", "HOLD"):
        return action
    return rec.original_action or rec.action


def _policy_rule_failed(rec: Recommendation, rule: str) -> bool:
    """True when ``rule`` is recorded as NOT passed in ``policy_checks_json``."""
    try:
        checks = json.loads(rec.policy_checks_json or "[]") or []
    except (TypeError, ValueError):
        return False
    return any(
        isinstance(c, dict) and c.get("rule") == rule and c.get("passed") is False
        for c in checks
    )


def _validator_blocked(rec: Recommendation, validator_analysis: Analysis | None) -> bool:
    """True when the VALIDATOR (not a policy rule) is what stopped the proposal.

    Two ways it blocks: an explicit VETO or a REVISE that names a different
    ``revised_action``; plus the indirect route — a confidence cut that makes the
    policy's ``min_confidence`` rule fail, which downgrades the action without the
    validator ever saying so.
    """
    if rec.validator_verdict == "VETO":
        return True
    if rec.validator_verdict != "REVISE":
        return False
    try:
        out = json.loads(validator_analysis.output_json or "{}") if validator_analysis else {}
    except (TypeError, ValueError):
        out = {}
    if not isinstance(out, dict):
        out = {}
    revised = out.get("revised_action")
    if revised and revised != _proposed_action(rec):
        return True
    adjustment = out.get("confidence_adjustment") or 0.0
    return adjustment < 0 and _policy_rule_failed(rec, "min_confidence")


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

        # --- validator: scored on the COUNTERFACTUAL whenever it blocked ---
        validator = by_agent.get("validator")
        validator_weight = _confidence_weight(
            validator.confidence if validator is not None else rec.confidence
        )
        proposed = _proposed_action(rec)
        if _validator_blocked(rec, validator) and proposed != rec.action:
            # What would the synthesizer's own proposal have earned? Blocking a
            # loser is a win; blocking a winner is an error. Previously this
            # check ran only for VETO — a verdict the validator never used — so
            # every REVISE got the easy credit of "the HOLD it forced was fine",
            # and a market that barely moves makes HOLD look right almost always.
            counter = _outcome_score(proposed, ret, item.norm)
            if counter <= 0.0:
                samples["validator"].append((True, validator_weight, None))
            elif counter > CORRECT_THRESHOLD:
                samples["validator"].append((False, validator_weight, None))
            # Grey zone (0 < counter <= threshold): the block changed nothing
            # worth scoring, so it contributes no sample rather than a coin flip.
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
                _EvalItem(rec=rec, ret=ret, score=score, correct=_is_correct(rec.action, score))
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
                    correct=_is_correct(rec.action, score),
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
    # Lessons are gated on batch size: they are injected into every future agent
    # prompt AND they deactivate the previous ones, so letting a 1-sample batch
    # rewrite them turns noise into a standing instruction. The floors are softer
    # than the statistical gate used for the displayed accuracy because lessons
    # are qualitative — but a single recommendation must never speak for an agent.
    n_symbols_scored = len({item.rec.symbol_id for item in items})
    if len(items) >= LESSONS_MIN_ITEMS and n_symbols_scored >= LESSONS_MIN_SYMBOLS:
        try:
            await feedback.generate_lessons(evaluation_id)
        except Exception:
            logger.exception("generate_lessons failed for evaluation %s", evaluation_id)
    else:
        logger.info(
            "Lezioni saltate per la valutazione %s: lotto troppo piccolo "
            "(%d campioni su %d titoli; minimo %d su %d)",
            evaluation_id,
            len(items),
            n_symbols_scored,
            LESSONS_MIN_ITEMS,
            LESSONS_MIN_SYMBOLS,
        )
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

    Returns ``{"pending_count", "ready_count", "awaiting_price_count",
    "next_evaluable_at"}``; ``next_evaluable_at`` is ``None`` when nothing is
    still maturing by the calendar.

    ``ready_count`` counts only what the next evaluation can ACTUALLY score:
    being 7 days old is necessary but not sufficient, because the evaluator also
    needs an entry price and a realized close for the target session. A
    recommendation whose 7-day mark falls on a weekend/holiday (or whose close
    simply has not been fetched yet) is calendar-mature but not yet scoreable,
    and is reported separately as ``awaiting_price_count`` — otherwise the UI
    would announce "N consigli maturi, verranno inclusi nella prossima
    valutazione" and then score none of them.
    """
    now = datetime.utcnow()
    cutoff = now - timedelta(days=EVALUATION_WINDOW_DAYS)

    rows = db.execute(
        select(
            Recommendation.created_at,
            Recommendation.symbol_id,
            Recommendation.entry_price,
        )
        .where(Recommendation.evaluated.is_(False))
        .order_by(Recommendation.created_at.asc())
    ).all()

    ready_count = 0
    awaiting_price_count = 0
    still_maturing: list[datetime] = []

    for created_at, symbol_id, entry_price in rows:
        if created_at > cutoff:
            still_maturing.append(created_at)
            continue
        # Calendar-mature: mirror the evaluator's own guards so the count can
        # never over-promise (see _run_weekly_evaluation_impl / _realized_return).
        target = created_at + timedelta(days=EVALUATION_WINDOW_DAYS)
        if (
            entry_price is not None
            and entry_price > 0
            and _first_close_at_or_after(db, symbol_id, target) is not None
        ):
            ready_count += 1
        else:
            awaiting_price_count += 1

    next_evaluable_at = (
        min(still_maturing) + timedelta(days=EVALUATION_WINDOW_DAYS) if still_maturing else None
    )

    # The evaluation runs a SECOND, horizon-aware pass too (see
    # _run_weekly_evaluation_impl): a recommendation already scored at 7 days can
    # still be waiting for its own horizon_days to mature. Counting it here is
    # what lets a caller tell whether a run would do any real work at all —
    # ``ready_count == 0`` alone would wrongly suggest "nothing to do".
    horizon_candidates = (
        db.execute(
            select(Recommendation).where(
                Recommendation.evaluated_h.is_(False),
                Recommendation.created_at <= now - timedelta(days=HORIZON_MIN_DAYS),
            )
        )
        .scalars()
        .all()
    )
    horizon_ready_count = 0
    for rec in horizon_candidates:
        window = _clamped_horizon(rec)
        if rec.created_at + timedelta(days=window) > now:
            continue  # its own horizon has not matured yet
        if rec.entry_price is None or rec.entry_price <= 0:
            continue
        target = rec.created_at + timedelta(days=window)
        if _first_close_at_or_after(db, rec.symbol_id, target) is None:
            continue
        horizon_ready_count += 1

    return {
        "pending_count": len(still_maturing),
        "ready_count": ready_count,
        "awaiting_price_count": awaiting_price_count,
        "horizon_ready_count": horizon_ready_count,
        "next_evaluable_at": next_evaluable_at,
    }
