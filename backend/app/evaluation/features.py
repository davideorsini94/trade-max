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

The module deliberately imports nothing from ``app.engine`` (or anything else in
the app) to stay free of import cycles — ``app.engine.orchestrator`` imports
*this* module, not the other way around.
"""

from __future__ import annotations

from typing import Any, Callable, NamedTuple

#: Bumped whenever the set/meaning of features changes, so a later evaluation can
#: tell snapshots of different vintages apart. Start at 1.
FEATURE_SCHEMA_VERSION = 1

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
