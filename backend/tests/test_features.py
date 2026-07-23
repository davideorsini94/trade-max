"""Offline tests for the deterministic feature snapshot (blueprint §7 addendum).

:func:`app.evaluation.features.build_feature_snapshot` is a pure function, so
these tests need neither the network nor a DB: they feed it hand-built ``data``
dicts shaped exactly like the one ``_gather_market_data`` assembles in the
orchestrator, and assert every raw value and every fixed-threshold bucket —
including the exact boundary values (VIX=20, correlation=0.75). A final
integration test drives the REAL ``_gather_market_data`` assembly (with faked
market/news/DB, mirroring test_estimates.py) to guard against silent drift
between the feature extractors' key names and the actual ``data`` shape.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

import app.data.market as market
import app.engine.orchestrator as orchestrator
from app.evaluation.features import (
    FEATURE_SCHEMA_VERSION,
    FEATURE_SPECS,
    build_feature_snapshot,
)


# --------------------------------------------------------------------------- #
# Builders for a realistic, fully-populated ``data`` dict
# --------------------------------------------------------------------------- #


def _full_data(
    *,
    vix_level: float = 16.9,
    max_corr: float | None = 0.62,
    corr_alert: bool = False,
) -> dict:
    """A ``data`` dict with every feature block populated with realistic values."""
    return {
        "relative_performance": {
            "relative_30d_pct": 3.2,
            "relative_90d_pct": -1.5,
            "relative_sector_30d_pct": 2.0,
            "relative_sector_90d_pct": -0.5,
        },
        "fundamentals": {
            "pe": 30.0,
            "beta": 1.1,
            "estimates": {
                "eps_current_year": {"revision_30d_pct": 1.2, "avg": 8.77},
                "eps_next_year": {"revision_30d_pct": -0.8, "avg": 9.71},
                "eps_revisions_up_30d": 5,
                "eps_revisions_down_30d": 2,
            },
        },
        "market_regime": {
            "vix_level": vix_level,
            "vix_change_30d_pct": -10.0,
            "yield_curve_10y_3m_spread_pct": 0.9,
            "credit_hyg_lqd_ratio_change_30d_pct": -1.2,
        },
        "risk_metrics": {
            "max_open_position_correlation_90d": max_corr,
            "correlation_alert": corr_alert,
            "distance_from_sma200_pct": 12.0,
            "drawdown_90d_pct": -8.0,
            "atr_pct": 2.5,
            "beta": 1.1,
            "days_to_next_earnings": 14,
        },
        "indicators": {"rsi14": 55.0, "volatility_30d_pct": 1.8},
    }


# --------------------------------------------------------------------------- #
# 1. Fully-populated snapshot: every value + every bucket correct
# --------------------------------------------------------------------------- #


def test_full_snapshot_values_and_buckets() -> None:
    snap = build_feature_snapshot(_full_data())

    assert snap["v"] == FEATURE_SCHEMA_VERSION

    # Relative strength (sign buckets).
    assert snap["rel_benchmark_30d_pct"] == pytest.approx(3.2)
    assert snap["rel_benchmark_30d_pct_bucket"] == "positivo"
    assert snap["rel_benchmark_90d_pct"] == pytest.approx(-1.5)
    assert snap["rel_benchmark_90d_pct_bucket"] == "negativo"
    assert snap["rel_sector_30d_pct"] == pytest.approx(2.0)
    assert snap["rel_sector_30d_pct_bucket"] == "positivo"
    assert snap["rel_sector_90d_pct"] == pytest.approx(-0.5)
    assert snap["rel_sector_90d_pct_bucket"] == "negativo"

    # Estimate revisions (sign buckets); net = up - down = 5 - 2 = 3.
    assert snap["eps_rev_cy_30d_pct"] == pytest.approx(1.2)
    assert snap["eps_rev_cy_30d_pct_bucket"] == "positivo"
    assert snap["eps_rev_ny_30d_pct"] == pytest.approx(-0.8)
    assert snap["eps_rev_ny_30d_pct_bucket"] == "negativo"
    assert snap["eps_rev_net_30d"] == 3
    assert snap["eps_rev_net_30d_bucket"] == "positivo"

    # Market regime.
    assert snap["vix_level"] == pytest.approx(16.9)
    assert snap["vix_level_bucket"] == "<20"
    assert snap["vix_change_30d_pct"] == pytest.approx(-10.0)
    assert snap["vix_change_30d_pct_bucket"] == "negativo"
    assert snap["yield_curve_10y_3m_spread_pct"] == pytest.approx(0.9)
    assert snap["yield_curve_10y_3m_spread_pct_bucket"] == "normale"
    assert snap["credit_hyg_lqd_ratio_change_30d_pct"] == pytest.approx(-1.2)
    assert snap["credit_hyg_lqd_ratio_change_30d_pct_bucket"] == "negativo"

    # Correlation.
    assert snap["max_open_position_correlation_90d"] == pytest.approx(0.62)
    assert snap["max_open_position_correlation_90d_bucket"] == "<0.75"
    assert snap["correlation_alert"] is False
    # An already-binary flag is stored raw, without a bucket key.
    assert "correlation_alert_bucket" not in snap

    # Baseline controls: raw only, no bucket keys.
    assert snap["rsi14"] == pytest.approx(55.0)
    assert snap["distance_from_sma200_pct"] == pytest.approx(12.0)
    assert snap["drawdown_90d_pct"] == pytest.approx(-8.0)
    assert snap["volatility_30d_pct"] == pytest.approx(1.8)
    assert snap["atr_pct"] == pytest.approx(2.5)
    assert snap["beta"] == pytest.approx(1.1)
    assert snap["days_to_next_earnings"] == 14
    for baseline in (
        "rsi14",
        "distance_from_sma200_pct",
        "drawdown_90d_pct",
        "volatility_30d_pct",
        "atr_pct",
        "beta",
        "days_to_next_earnings",
    ):
        assert f"{baseline}_bucket" not in snap


# --------------------------------------------------------------------------- #
# 1b. Exact-boundary values must land on the documented side
# --------------------------------------------------------------------------- #


def test_boundary_buckets() -> None:
    # VIX = 20 exactly -> "20-30" (middle band inclusive on both edges).
    assert build_feature_snapshot(_full_data(vix_level=20.0))["vix_level_bucket"] == "20-30"
    # VIX = 30 exactly -> "20-30".
    assert build_feature_snapshot(_full_data(vix_level=30.0))["vix_level_bucket"] == "20-30"
    # VIX = 30.01 -> ">30".
    assert build_feature_snapshot(_full_data(vix_level=30.01))["vix_level_bucket"] == ">30"
    # VIX = 19.99 -> "<20".
    assert build_feature_snapshot(_full_data(vix_level=19.99))["vix_level_bucket"] == "<20"

    # Correlation = 0.75 exactly -> ">=0.75" (inclusive high side, matches the
    # concentration-risk alert threshold).
    snap = build_feature_snapshot(_full_data(max_corr=0.75))
    assert snap["max_open_position_correlation_90d_bucket"] == ">=0.75"
    # Just below -> "<0.75".
    snap = build_feature_snapshot(_full_data(max_corr=0.7499))
    assert snap["max_open_position_correlation_90d_bucket"] == "<0.75"


def test_sign_bucket_zero_is_neutro() -> None:
    # up == down -> net 0 -> a distinct "neutro" bucket (not lumped into a sign).
    data = _full_data()
    data["fundamentals"]["estimates"]["eps_revisions_up_30d"] = 4
    data["fundamentals"]["estimates"]["eps_revisions_down_30d"] = 4
    snap = build_feature_snapshot(data)
    assert snap["eps_rev_net_30d"] == 0
    assert snap["eps_rev_net_30d_bucket"] == "neutro"

    # A flat yield curve (exactly 0) is "normale", not "invertita".
    data = _full_data()
    data["market_regime"]["yield_curve_10y_3m_spread_pct"] = 0.0
    snap = build_feature_snapshot(data)
    assert snap["yield_curve_10y_3m_spread_pct_bucket"] == "normale"


# --------------------------------------------------------------------------- #
# 2. Sources present but empty: estimates=None, regime all None, no correlation
# --------------------------------------------------------------------------- #


def test_missing_sources_yield_none_and_missing_buckets() -> None:
    data = {
        "relative_performance": {
            "relative_30d_pct": None,
            "relative_90d_pct": None,
            "relative_sector_30d_pct": None,
            "relative_sector_90d_pct": None,
        },
        # estimates explicitly None (ETF / non-US ticker with no snapshot).
        "fundamentals": {"pe": None, "estimates": None},
        "market_regime": market._empty_market_regime(),
        # risk_metrics WITHOUT the correlation keys (no open positions) and with
        # the baseline fields all None.
        "risk_metrics": {
            "distance_from_sma200_pct": None,
            "drawdown_90d_pct": None,
            "atr_pct": None,
            "beta": None,
            "days_to_next_earnings": None,
        },
        "indicators": {"rsi14": None, "volatility_30d_pct": None},
    }

    snap = build_feature_snapshot(data)

    assert snap["v"] == FEATURE_SCHEMA_VERSION

    # Relative strength -> None + "mancante".
    for name in (
        "rel_benchmark_30d_pct",
        "rel_benchmark_90d_pct",
        "rel_sector_30d_pct",
        "rel_sector_90d_pct",
    ):
        assert snap[name] is None
        assert snap[f"{name}_bucket"] == "mancante"

    # Estimate revisions -> None + "mancante".
    for name in ("eps_rev_cy_30d_pct", "eps_rev_ny_30d_pct", "eps_rev_net_30d"):
        assert snap[name] is None
        assert snap[f"{name}_bucket"] == "mancante"

    # Regime -> None + the appropriate "missing" label per bucket kind.
    assert snap["vix_level"] is None
    assert snap["vix_level_bucket"] == "mancante"
    assert snap["vix_change_30d_pct"] is None
    assert snap["vix_change_30d_pct_bucket"] == "mancante"
    assert snap["yield_curve_10y_3m_spread_pct"] is None
    assert snap["yield_curve_10y_3m_spread_pct_bucket"] == "mancante"
    assert snap["credit_hyg_lqd_ratio_change_30d_pct"] is None
    assert snap["credit_hyg_lqd_ratio_change_30d_pct_bucket"] == "mancante"

    # No open position -> None + the distinct "nessuna_posizione" label.
    assert snap["max_open_position_correlation_90d"] is None
    assert snap["max_open_position_correlation_90d_bucket"] == "nessuna_posizione"
    # correlation_alert key absent from risk_metrics -> stored as None (raw).
    assert snap["correlation_alert"] is None

    # Baselines -> None.
    for name in (
        "rsi14",
        "distance_from_sma200_pct",
        "drawdown_90d_pct",
        "volatility_30d_pct",
        "atr_pct",
        "beta",
        "days_to_next_earnings",
    ):
        assert snap[name] is None


# --------------------------------------------------------------------------- #
# 3. Completely empty ``data`` -> no exception, all None, "v" present
# --------------------------------------------------------------------------- #


def test_empty_data_never_raises_and_all_none() -> None:
    snap = build_feature_snapshot({})

    assert snap["v"] == FEATURE_SCHEMA_VERSION

    for spec in FEATURE_SPECS:
        assert snap[spec.name] is None
        if spec.bucket is not None:
            # Present, and one of the recognized "missing"-type labels.
            assert snap[f"{spec.name}_bucket"] in {"mancante", "nessuna_posizione"}

    # Robust even against a non-dict input.
    assert build_feature_snapshot(None)["v"] == FEATURE_SCHEMA_VERSION  # type: ignore[arg-type]

    # The whole snapshot must be JSON-serializable (it is persisted as a string).
    assert json.loads(json.dumps(snap)) == snap


# --------------------------------------------------------------------------- #
# 4. Net revisions: one side missing -> None (never a partial estimate)
# --------------------------------------------------------------------------- #


def test_eps_rev_net_requires_both_sides() -> None:
    # up present, down missing.
    data = _full_data()
    data["fundamentals"]["estimates"]["eps_revisions_up_30d"] = 5
    data["fundamentals"]["estimates"]["eps_revisions_down_30d"] = None
    snap = build_feature_snapshot(data)
    assert snap["eps_rev_net_30d"] is None
    assert snap["eps_rev_net_30d_bucket"] == "mancante"

    # down present, up missing.
    data = _full_data()
    data["fundamentals"]["estimates"]["eps_revisions_up_30d"] = None
    data["fundamentals"]["estimates"]["eps_revisions_down_30d"] = 3
    snap = build_feature_snapshot(data)
    assert snap["eps_rev_net_30d"] is None
    assert snap["eps_rev_net_30d_bucket"] == "mancante"


# --------------------------------------------------------------------------- #
# 5. Integration: the REAL _gather_market_data assembly -> snapshot -> JSON
# --------------------------------------------------------------------------- #


class _FakeDB:
    def get(self, model: object, ident: object) -> None:
        return None


@contextmanager
def _fake_session_scope():
    yield _FakeDB()


def _trending_history(n: int = 260) -> pd.DataFrame:
    """A gently-trending OHLCV frame long enough for sma200/rsi/vol to compute."""
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    base = np.linspace(100.0, 130.0, n) + np.sin(np.linspace(0, 12, n))
    return pd.DataFrame(
        {
            "open": base,
            "high": base + 1.0,
            "low": base - 1.0,
            "close": base,
            "volume": np.full(n, 1_000_000.0),
        },
        index=idx,
    )


class _FakeMarket:
    """Minimal MarketDataService double for :func:`_gather_market_data`."""

    def get_history_df(self, *a: object, **k: object) -> pd.DataFrame:
        return _trending_history()

    def price_summary(self, df: pd.DataFrame) -> dict:
        return {"close": 130.0, "change_pct_30d": 4.0, "change_pct_90d": 6.0}

    def get_fundamentals(self, *a: object, **k: object) -> dict:
        return {"pe": 20.0, "beta": 1.15, "sector": "Technology"}

    def get_analyst_estimates(self, *a: object, **k: object) -> dict:
        est = market._empty_estimates()
        est["eps_current_year"] = {
            "avg": 8.77,
            "n_analysts": 41,
            "growth_pct": 17.5,
            "revision_7d_pct": 0.1,
            "revision_30d_pct": 1.3,
            "revision_90d_pct": 3.0,
        }
        est["eps_revisions_up_30d"] = 7
        est["eps_revisions_down_30d"] = 2
        return est

    def get_sentiment_snapshot(self, *a: object, **k: object) -> dict:
        snap = market._empty_sentiment()
        snap["days_to_earnings"] = 21
        return snap

    def get_market_regime(self, *a: object, **k: object) -> dict:
        regime = market._empty_market_regime()
        regime["vix_level"] = 22.5
        regime["vix_change_30d_pct"] = 8.0
        regime["yield_curve_10y_3m_spread_pct"] = -0.3
        regime["credit_hyg_lqd_ratio_change_30d_pct"] = -0.5
        return regime

    def get_relative_performance(
        self,
        ticker: str,
        change_30d: float | None,
        change_90d: float | None,
        sector: str | None = None,
    ) -> dict:
        return {
            "relative_30d_pct": 2.5,
            "relative_90d_pct": -1.0,
            "relative_sector_30d_pct": 1.5,
            "relative_sector_90d_pct": -0.5,
            "sector": sector,
            "sector_etf": "XLK",
        }


class _FakeNews:
    def fetch_macro(self, *a: object, **k: object) -> list:
        return []

    def fetch_corporate(self, *a: object, **k: object) -> list:
        return []


def test_snapshot_from_real_gather_market_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orchestrator, "market_data_service", _FakeMarket())
    monkeypatch.setattr(orchestrator, "news_service", _FakeNews())
    monkeypatch.setattr(orchestrator, "session_scope", _fake_session_scope)

    data = orchestrator._gather_market_data(symbol_id=1, ticker="AAPL")
    snap = build_feature_snapshot(data)

    # Persisted as a JSON string on the Recommendation; must round-trip.
    assert json.loads(json.dumps(snap, ensure_ascii=False)) == snap
    assert snap["v"] == FEATURE_SCHEMA_VERSION

    # Feature extractors line up with the REAL data-dict key names.
    assert snap["rel_benchmark_30d_pct"] == pytest.approx(2.5)
    assert snap["rel_benchmark_30d_pct_bucket"] == "positivo"
    assert snap["rel_sector_90d_pct_bucket"] == "negativo"

    assert snap["eps_rev_cy_30d_pct"] == pytest.approx(1.3)
    assert snap["eps_rev_cy_30d_pct_bucket"] == "positivo"
    assert snap["eps_rev_net_30d"] == 5  # 7 - 2
    assert snap["eps_rev_net_30d_bucket"] == "positivo"

    assert snap["vix_level"] == pytest.approx(22.5)
    assert snap["vix_level_bucket"] == "20-30"
    assert snap["yield_curve_10y_3m_spread_pct_bucket"] == "invertita"

    # No open positions in the fake DB -> correlation is None / nessuna_posizione,
    # and the alert is a real bool (False), not None.
    assert snap["max_open_position_correlation_90d"] is None
    assert snap["max_open_position_correlation_90d_bucket"] == "nessuna_posizione"
    assert snap["correlation_alert"] is False

    # Baselines actually computed from the trending history / risk metrics.
    assert snap["rsi14"] is not None
    assert snap["volatility_30d_pct"] is not None
    assert snap["distance_from_sma200_pct"] is not None
    assert snap["beta"] == pytest.approx(1.15)
    assert snap["days_to_next_earnings"] == 21
