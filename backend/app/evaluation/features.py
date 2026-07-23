"""Deterministic feature snapshot persisted on every ``Recommendation``.

This module builds a flat, versioned dict of the deterministic signals that fed
a run — the recently-added ones (sector relative strength, analyst-estimate
revisions, market regime, portfolio correlation) plus a handful of already-known
baseline indicators used as a control group. The snapshot is stored verbatim on
``Recommendation.features_json`` so a later weekly evaluation can test, over time
and across many recommendations, whether those new signals were actually
predictive (blueprint §7 addendum, part 1 of 4).

Design notes
------------
* :func:`build_feature_snapshot` is **pure** — no I/O, no DB, no network — and
  never raises: every feature is extracted inside its own guard and falls back
  to ``None`` when the source block is missing (an ETF has no estimates, a
  non-US ticker often has no sector ETF, a first-ever run has no open positions).
  This mirrors the "every key always present, ``None`` when the source is
  absent" convention already used by ``_empty_market_regime`` /
  ``_empty_estimates`` in ``app.data.market``.
* Buckets use **fixed domain thresholds**, never sample quantiles: with only a
  handful of recommendations per week, quantile edges would be unstable and make
  week-over-week buckets incomparable. Thresholds match those already used
  elsewhere in the pipeline (e.g. the correlation-alert threshold of 0.75 in
  ``app.engine.orchestrator``).
* Each feature records its originating agent, so a future per-agent coaching
  loop can attribute predictive power back to the analyst that produced it.

The module deliberately imports nothing from ``app.engine`` to stay free of
import cycles — ``app.engine.orchestrator`` imports *this* module, not the
other way around. It does import :class:`~app.models.Recommendation` (for
:func:`compute_feature_stats`'s DB query), which is safe: models never import
back into ``app.evaluation``.
"""

from __future__ import annotations

import json
from typing import Any, Callable, NamedTuple

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Recommendation

#: Bumped whenever the set/meaning of features changes, so a later evaluation can
#: tell snapshots of different vintages apart. Start at 1.
FEATURE_SCHEMA_VERSION = 1

#: A bucket (or the rank-IC below) is only judged "ok" once it clears BOTH
#: floors — enough samples, spread across enough distinct symbols that it
#: isn't really just one favorite's repeated history in disguise (see the
#: dedupe-by-symbol-week in :func:`compute_feature_stats`).
FEATURE_STATS_MIN_N = 12
FEATURE_STATS_MIN_SYMBOLS = 4
#: Spearman rank-IC needs a somewhat larger sample than a simple bucket split
#: to be worth reporting at all.
FEATURE_STATS_MIN_N_FOR_IC = 20

# Agent-of-origin labels (kept in sync with the pipeline actor names).
_AGENT_TECHNICAL = "technical"
_AGENT_FUNDAMENTALS = "fundamentals"
_AGENT_MACRO_NEWS = "macro_news"
_AGENT_VALIDATOR = "validator"

# Correlation-alert threshold, mirrored from ``app.engine.orchestrator``
# (``_CORRELATION_ALERT_THRESHOLD``): a correlation AT or above this raises the
# concentration-risk flag, so the bucket boundary must include equality on the
# high side (``>=0.75``) to stay consistent.
_CORRELATION_ALERT_THRESHOLD = 0.75


# --------------------------------------------------------------------------- #
# Nested-dict access
# --------------------------------------------------------------------------- #


def _get(data: Any, *keys: str) -> Any:
    """Walk nested dicts by ``keys``; return ``None`` if any level is absent.

    Any non-dict encountered mid-walk (including ``None`` — e.g. an absent
    ``estimates`` block, or ``eps_current_year`` being ``None`` rather than a
    dict of ``None``s) short-circuits to ``None`` instead of raising.
    """
    node = data
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


# --------------------------------------------------------------------------- #
# Bucket functions (fixed domain thresholds, total over their input)
# --------------------------------------------------------------------------- #


def _sign_bucket(value: Any) -> str:
    """Sign bucket: ``"positivo"`` (>0), ``"negativo"`` (<0), ``"neutro"`` (==0).

    ``None`` (source missing) maps to ``"mancante"``. Exactly-zero is its own
    ``"neutro"`` bucket rather than being lumped in with either sign: for a
    net-revision count (``up - down``) a value of 0 genuinely means "balanced",
    not "negative".
    """
    if value is None:
        return "mancante"
    if value > 0:
        return "positivo"
    if value < 0:
        return "negativo"
    return "neutro"


def _vix_bucket(value: Any) -> str:
    """VIX regime: ``"<20"`` calm, ``"20-30"`` elevated, ``">30"`` stressed.

    The boundaries are inclusive on the middle band (``20 <= x <= 30``), so an
    exact 20 or 30 lands in ``"20-30"``. ``None`` maps to ``"mancante"``.
    """
    if value is None:
        return "mancante"
    if value < 20:
        return "<20"
    if value > 30:
        return ">30"
    return "20-30"


def _yield_curve_bucket(value: Any) -> str:
    """Yield curve: ``"invertita"`` (<0) vs ``"normale"`` (>=0); ``None`` -> ``"mancante"``.

    A negative 10y-3m spread is an inverted curve (classic recession signal); a
    flat curve (exactly 0) is treated as ``"normale"``.
    """
    if value is None:
        return "mancante"
    return "invertita" if value < 0 else "normale"


def _correlation_bucket(value: Any) -> str:
    """Max open-position correlation: ``">=0.75"`` / ``"<0.75"`` / ``"nessuna_posizione"``.

    ``None`` means there was no open position to correlate against (or too little
    overlapping history), not a missing measurement, hence the distinct
    ``"nessuna_posizione"`` label. The 0.75 boundary is inclusive on the high
    side to match the concentration-risk alert.
    """
    if value is None:
        return "nessuna_posizione"
    return ">=0.75" if value >= _CORRELATION_ALERT_THRESHOLD else "<0.75"


# --------------------------------------------------------------------------- #
# Feature specifications
# --------------------------------------------------------------------------- #


class FeatureSpec(NamedTuple):
    """One deterministic feature.

    ``extract`` pulls the raw value out of the ``data`` dict (or returns
    ``None``); ``bucket`` maps that raw value to a fixed-threshold category
    string, or is ``None`` for features stored raw-only (the baseline control
    indicators, and the already-binary ``correlation_alert``).
    """

    name: str
    agent: str
    extract: Callable[[dict], Any]
    bucket: Callable[[Any], str] | None = None


def _eps_rev_net_30d(data: dict) -> Any:
    """Net 30-day EPS revisions = up - down, only when BOTH counts are present.

    Never estimated from a single side: if either ``eps_revisions_up_30d`` or
    ``eps_revisions_down_30d`` is missing, the net is ``None``.
    """
    up = _get(data, "fundamentals", "estimates", "eps_revisions_up_30d")
    down = _get(data, "fundamentals", "estimates", "eps_revisions_down_30d")
    if up is None or down is None:
        return None
    return up - down


#: Ordered feature specs. Order is stable so snapshots are diff-friendly and the
#: JSON key order is deterministic across runs.
FEATURE_SPECS: tuple[FeatureSpec, ...] = (
    # --- Sector / benchmark relative strength (technical) --------------------
    FeatureSpec(
        "rel_benchmark_30d_pct",
        _AGENT_TECHNICAL,
        lambda d: _get(d, "relative_performance", "relative_30d_pct"),
        _sign_bucket,
    ),
    FeatureSpec(
        "rel_benchmark_90d_pct",
        _AGENT_TECHNICAL,
        lambda d: _get(d, "relative_performance", "relative_90d_pct"),
        _sign_bucket,
    ),
    FeatureSpec(
        "rel_sector_30d_pct",
        _AGENT_TECHNICAL,
        lambda d: _get(d, "relative_performance", "relative_sector_30d_pct"),
        _sign_bucket,
    ),
    FeatureSpec(
        "rel_sector_90d_pct",
        _AGENT_TECHNICAL,
        lambda d: _get(d, "relative_performance", "relative_sector_90d_pct"),
        _sign_bucket,
    ),
    # --- Analyst-estimate revisions (fundamentals) --------------------------
    FeatureSpec(
        "eps_rev_cy_30d_pct",
        _AGENT_FUNDAMENTALS,
        lambda d: _get(d, "fundamentals", "estimates", "eps_current_year", "revision_30d_pct"),
        _sign_bucket,
    ),
    FeatureSpec(
        "eps_rev_ny_30d_pct",
        _AGENT_FUNDAMENTALS,
        lambda d: _get(d, "fundamentals", "estimates", "eps_next_year", "revision_30d_pct"),
        _sign_bucket,
    ),
    FeatureSpec(
        "eps_rev_net_30d",
        _AGENT_FUNDAMENTALS,
        _eps_rev_net_30d,
        _sign_bucket,
    ),
    # --- Market regime (macro_news) -----------------------------------------
    FeatureSpec(
        "vix_level",
        _AGENT_MACRO_NEWS,
        lambda d: _get(d, "market_regime", "vix_level"),
        _vix_bucket,
    ),
    FeatureSpec(
        "vix_change_30d_pct",
        _AGENT_MACRO_NEWS,
        lambda d: _get(d, "market_regime", "vix_change_30d_pct"),
        _sign_bucket,
    ),
    FeatureSpec(
        "yield_curve_10y_3m_spread_pct",
        _AGENT_MACRO_NEWS,
        lambda d: _get(d, "market_regime", "yield_curve_10y_3m_spread_pct"),
        _yield_curve_bucket,
    ),
    FeatureSpec(
        "credit_hyg_lqd_ratio_change_30d_pct",
        _AGENT_MACRO_NEWS,
        lambda d: _get(d, "market_regime", "credit_hyg_lqd_ratio_change_30d_pct"),
        _sign_bucket,
    ),
    # --- Portfolio correlation risk (validator) -----------------------------
    FeatureSpec(
        "max_open_position_correlation_90d",
        _AGENT_VALIDATOR,
        lambda d: _get(d, "risk_metrics", "max_open_position_correlation_90d"),
        _correlation_bucket,
    ),
    FeatureSpec(
        # Already binary — stored raw, no bucket.
        "correlation_alert",
        _AGENT_VALIDATOR,
        lambda d: _get(d, "risk_metrics", "correlation_alert"),
        None,
    ),
    # --- Baseline control indicators (already known; raw-only) --------------
    # These are the yardstick: the evaluation judges whether the NEW features
    # above discriminate outcomes better than these long-standing indicators.
    FeatureSpec(
        "rsi14",
        _AGENT_TECHNICAL,
        lambda d: _get(d, "indicators", "rsi14"),
        None,
    ),
    FeatureSpec(
        "distance_from_sma200_pct",
        _AGENT_TECHNICAL,
        lambda d: _get(d, "risk_metrics", "distance_from_sma200_pct"),
        None,
    ),
    FeatureSpec(
        "drawdown_90d_pct",
        _AGENT_TECHNICAL,
        lambda d: _get(d, "risk_metrics", "drawdown_90d_pct"),
        None,
    ),
    FeatureSpec(
        "volatility_30d_pct",
        _AGENT_TECHNICAL,
        lambda d: _get(d, "indicators", "volatility_30d_pct"),
        None,
    ),
    FeatureSpec(
        "atr_pct",
        _AGENT_TECHNICAL,
        lambda d: _get(d, "risk_metrics", "atr_pct"),
        None,
    ),
    FeatureSpec(
        "beta",
        _AGENT_TECHNICAL,
        lambda d: _get(d, "risk_metrics", "beta"),
        None,
    ),
    FeatureSpec(
        "days_to_next_earnings",
        _AGENT_TECHNICAL,
        lambda d: _get(d, "risk_metrics", "days_to_next_earnings"),
        None,
    ),
)


def build_feature_snapshot(data: dict) -> dict:
    """Build the deterministic feature snapshot for one run's ``data`` dict.

    ``data`` is the dict returned by ``_gather_market_data`` in the orchestrator.
    Returns a flat dict with ``"v": FEATURE_SCHEMA_VERSION`` plus, for every
    declared feature, its raw value under the feature name and — for bucketed
    features — its category under ``"<name>_bucket"``. Every key is ALWAYS
    present; a value is ``None`` (and its bucket the appropriate "missing"
    label) whenever the source block is absent. Never raises.
    """
    if not isinstance(data, dict):
        data = {}
    snapshot: dict[str, Any] = {"v": FEATURE_SCHEMA_VERSION}
    for spec in FEATURE_SPECS:
        try:
            raw = spec.extract(data)
        except Exception:
            raw = None
        snapshot[spec.name] = raw
        if spec.bucket is not None:
            try:
                snapshot[f"{spec.name}_bucket"] = spec.bucket(raw)
            except Exception:
                snapshot[f"{spec.name}_bucket"] = None
    return snapshot


# --------------------------------------------------------------------------- #
# Cumulative feature validation (blueprint §7 addendum, part 3 of 4)
# --------------------------------------------------------------------------- #
# Tests, across every recommendation that has matured at its OWN horizon, and
# cumulatively over time (not just the current week), whether each feature
# above actually says anything about the recommendation's return relative to
# its benchmark — instead of assuming it does just because it looked
# reasonable when it was added. Purely deterministic Python (pandas only, no
# LLM, no network); the honesty gate below means most features will read
# "dati_insufficienti" for weeks after this ships, and rare regimes (VIX>30,
# an active correlation_alert) may stay that way for months — that is the
# correct, non-invented answer, not a bug.


def _week_key(created_at) -> tuple[int, int]:
    """(ISO year, ISO week) for ``created_at`` — the dedupe granularity below."""
    iso = created_at.isocalendar()
    return (iso[0], iso[1])


def _dedupe_by_symbol_week(recs: list[Recommendation]) -> list[Recommendation]:
    """One sample per (symbol, ISO week): the earliest recommendation in it.

    A favorite re-analysed several times a day would otherwise contribute many
    near-identical, overlapping-outcome "samples" of the same underlying signal,
    making any bucket look far more (or less) significant than it really is.
    """
    earliest: dict[tuple[int, int, int], Recommendation] = {}
    for rec in sorted(recs, key=lambda r: r.created_at):
        key = (rec.symbol_id, *_week_key(rec.created_at))
        if key not in earliest:
            earliest[key] = rec
    return list(earliest.values())


def _feature_stat(
    spec: FeatureSpec, parsed: list[tuple[dict, float, int]]
) -> dict[str, Any]:
    """Bucket-level accuracy/avg-excess-return, plus a Spearman rank-IC, for one feature.

    ``parsed`` is ``[(snapshot, excess_return_h, symbol_id), ...]`` for the
    deduplicated cohort. "accuracy" here means "share of samples in this
    bucket with a POSITIVE excess return" — a market-relative, action-agnostic
    question distinct from ``evaluator._outcome_score``'s action-specific
    correctness. Every result carries its own ``n``/``n_symbols`` so a reader
    can judge the gating themselves; a bucket/IC below the floor is reported as
    ``"dati_insufficienti"`` together with those counts, never silently hidden
    and never guessed at.
    """
    out: dict[str, Any] = {}

    if spec.bucket is not None:
        buckets: dict[str, list[tuple[float, int]]] = {}
        for snapshot, excess, symbol_id in parsed:
            bucket_name = snapshot.get(f"{spec.name}_bucket")
            if bucket_name is None:
                continue
            buckets.setdefault(bucket_name, []).append((excess, symbol_id))

        bucket_out: dict[str, Any] = {}
        for bucket_name, values in buckets.items():
            n = len(values)
            n_symbols = len({symbol_id for _excess, symbol_id in values})
            if n >= FEATURE_STATS_MIN_N and n_symbols >= FEATURE_STATS_MIN_SYMBOLS:
                bucket_out[bucket_name] = {
                    "status": "ok",
                    "n": n,
                    "n_symbols": n_symbols,
                    "accuracy": round(sum(1 for e, _s in values if e > 0) / n, 4),
                    "avg_excess_return_pct": round(sum(e for e, _s in values) / n, 4),
                }
            else:
                bucket_out[bucket_name] = {
                    "status": "dati_insufficienti",
                    "n": n,
                    "n_symbols": n_symbols,
                }
        out["buckets"] = bucket_out

    # Spearman rank-IC (feature's raw value -> excess return): computed for ANY
    # numeric feature, bucketed or not (the baseline control indicators — RSI,
    # drawdown, beta... — have no bucket, so this is their ONLY validation).
    numeric_pairs: list[tuple[float, float]] = []
    for snapshot, excess, _symbol_id in parsed:
        value = snapshot.get(spec.name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue  # skip None, and non-numeric/boolean features (correlation_alert)
        numeric_pairs.append((float(value), excess))

    if len(numeric_pairs) >= FEATURE_STATS_MIN_N_FOR_IC:
        xs = pd.Series(v for v, _ in numeric_pairs)
        ys = pd.Series(v for _, v in numeric_pairs)
        # Spearman's rho = the Pearson correlation of the two series' RANKS.
        # pandas' own method="spearman" needs scipy under the hood (not a
        # project dependency); ranking first and taking the default (Pearson)
        # correlation is the standard scipy-free way to get the same number.
        ic = xs.rank().corr(ys.rank())
        out["rank_ic"] = (
            {"status": "ok", "n": len(numeric_pairs), "value": round(float(ic), 4)}
            if not pd.isna(ic)
            else {"status": "dati_insufficienti", "n": len(numeric_pairs)}
        )
    else:
        out["rank_ic"] = {"status": "dati_insufficienti", "n": len(numeric_pairs)}

    return out


def compute_feature_stats(db: Session) -> dict:
    """Cumulative per-feature validation across every matured recommendation.

    Cohort: every ``Recommendation`` with ``evaluated_h=True``, a
    ``features_json`` snapshot, AND a resolvable ``excess_return_h`` (rows
    without a benchmark are excluded HERE specifically — this measures
    market-RELATIVE predictive power, so mixing in absolute-return rows would
    blur the comparison; those rows are still scored individually via their own
    ``outcome_score_h`` elsewhere), deduplicated to one sample per
    (symbol, ISO week) via :func:`_dedupe_by_symbol_week`. Pure and
    deterministic; never raises (a malformed ``features_json`` row is skipped,
    not fatal).
    """
    rows = (
        db.execute(
            select(Recommendation).where(
                Recommendation.evaluated_h.is_(True),
                Recommendation.features_json.isnot(None),
            )
        )
        .scalars()
        .all()
    )
    cohort = _dedupe_by_symbol_week(rows)

    result: dict[str, Any] = {
        "cohort_n": len(cohort),
        "cohort_n_before_dedupe": len(rows),
    }
    if not cohort:
        result["note"] = "Nessuna raccomandazione con snapshot di feature ancora matura."
        result["features"] = {}
        return result

    parsed: list[tuple[dict, float, int]] = []
    for rec in cohort:
        if rec.excess_return_h is None:
            continue
        try:
            snapshot = json.loads(rec.features_json)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(snapshot, dict):
            parsed.append((snapshot, rec.excess_return_h, rec.symbol_id))

    result["excess_return_n"] = len(parsed)
    if not parsed:
        result["note"] = (
            "Nessuna raccomandazione matura ha un rendimento in eccesso "
            "risolvibile rispetto al benchmark (dati di mercato insufficienti)."
        )
        result["features"] = {}
        return result

    result["features"] = {spec.name: _feature_stat(spec, parsed) for spec in FEATURE_SPECS}
    return result
