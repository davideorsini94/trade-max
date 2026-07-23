"""Offline tests for the global market-regime snapshot and its wiring.

Everything that would touch yfinance is monkeypatched (``app.data.market.yf.Ticker``),
so these tests never hit the network. Fake histories are built with just enough rows
(31) for the 30-session lookback arithmetic to produce a value instead of None.
"""

from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
import pytest

import app.data.market as market
import app.engine.orchestrator as orchestrator
from app.agents.base import AgentContext
from app.agents.macro_news import MacroNewsAnalystAgent
from app.engine.policy import MarketMetrics
from app.schemas import SymbolOut


def _symbol(ticker: str = "AAPL") -> SymbolOut:
    return SymbolOut(
        id=1,
        ticker=ticker,
        name="Apple Inc.",
        exchange="NASDAQ",
        currency="USD",
        asset_type="EQUITY",
        is_favorite=False,
        is_active=True,
        created_at=datetime.utcnow(),
    )


def _history(closes: list[float]) -> pd.DataFrame:
    """A fake yfinance ``history()`` frame: a DatetimeIndex and a "Close" column."""
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="D")
    return pd.DataFrame({"Close": closes}, index=idx)


# 31 rows: iloc[-31] (30 sessions back) is the first element, iloc[-1] the last.
_VIX_SERIES = [20.0] + [15.0] * 29 + [16.91]
_TREASURY_10Y_SERIES = [4.30] + [4.5] * 29 + [4.65]
_TREASURY_3M_SERIES = [3.60] + [3.7] * 29 + [3.745]
_HYG_SERIES = [90.0] + [88.0] * 29 + [86.4]
_LQD_SERIES = [110.0] + [109.0] * 29 + [108.0]


class _FakeTicker:
    def __init__(self, history_df: pd.DataFrame | None, *, raises: bool = False) -> None:
        self._history_df = history_df
        self._raises = raises

    def history(self, **kwargs: object) -> pd.DataFrame:
        if self._raises:
            raise RuntimeError("boom")
        return self._history_df


def _install_fake_tickers(
    monkeypatch: pytest.MonkeyPatch, histories: dict[str, pd.DataFrame | None], *, raises: frozenset[str] = frozenset()
) -> dict[str, int]:
    """Monkeypatch yf.Ticker to serve canned histories keyed by yfinance symbol.

    Returns a call-counter dict (keyed by ticker symbol) so cache-hit tests can
    assert no new fetch happened.
    """
    calls: dict[str, int] = {}

    def _factory(symbol: str, *a: object, **k: object) -> _FakeTicker:
        calls[symbol] = calls.get(symbol, 0) + 1
        return _FakeTicker(histories.get(symbol), raises=symbol in raises)

    monkeypatch.setattr(market.yf, "Ticker", _factory)
    return calls


def _all_regime_histories(**overrides: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Full set of canned histories for every _REGIME_TICKERS symbol, with overrides."""
    base = {
        "^VIX": _history(_VIX_SERIES),
        "^TNX": _history(_TREASURY_10Y_SERIES),
        "^IRX": _history(_TREASURY_3M_SERIES),
        "EURUSD=X": _history([1.10] + [1.12] * 29 + [1.141]),
        "GC=F": _history([2000.0] + [2050.0] * 29 + [2100.0]),
        "CL=F": _history([75.0] + [78.0] * 29 + [80.0]),
        "HYG": _history(_HYG_SERIES),
        "LQD": _history(_LQD_SERIES),
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# 1. Every instrument fails -> all-None snapshot, not cached
# --------------------------------------------------------------------------- #


def test_get_market_regime_all_failures_returns_all_none_and_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    market._market_regime_cache.clear()
    calls = _install_fake_tickers(
        monkeypatch, {}, raises=frozenset(market._REGIME_TICKERS.values())
    )

    result = market.market_data_service.get_market_regime()

    assert result == market._empty_market_regime()
    assert market._MARKET_REGIME_CACHE_KEY not in market._market_regime_cache

    # Not cached: a second call retries every ticker.
    market.market_data_service.get_market_regime()
    assert all(count == 2 for count in calls.values())


# --------------------------------------------------------------------------- #
# 2. VIX level + 30-session change computed correctly
# --------------------------------------------------------------------------- #


def test_get_market_regime_vix_level_and_change(monkeypatch: pytest.MonkeyPatch) -> None:
    market._market_regime_cache.clear()
    _install_fake_tickers(monkeypatch, _all_regime_histories())

    result = market.market_data_service.get_market_regime()

    assert result["vix_level"] == pytest.approx(16.91)
    assert result["vix_change_30d_pct"] == pytest.approx((16.91 - 20.0) / 20.0 * 100.0)


# --------------------------------------------------------------------------- #
# 3. HYG/LQD credit-spread proxy computed on the ratio, not the difference
# --------------------------------------------------------------------------- #


def test_get_market_regime_credit_ratio_change(monkeypatch: pytest.MonkeyPatch) -> None:
    market._market_regime_cache.clear()
    _install_fake_tickers(monkeypatch, _all_regime_histories())

    result = market.market_data_service.get_market_regime()

    first_ratio = _HYG_SERIES[0] / _LQD_SERIES[0]
    last_ratio = _HYG_SERIES[-1] / _LQD_SERIES[-1]
    expected = (last_ratio - first_ratio) / first_ratio * 100.0
    assert result["credit_hyg_lqd_ratio_change_30d_pct"] == pytest.approx(expected)


# --------------------------------------------------------------------------- #
# 4. Second call within the TTL is served from cache (no new fetches)
# --------------------------------------------------------------------------- #


def test_get_market_regime_second_call_served_from_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    market._market_regime_cache.clear()
    calls = _install_fake_tickers(monkeypatch, _all_regime_histories())

    first = market.market_data_service.get_market_regime()
    second = market.market_data_service.get_market_regime()

    assert first == second
    assert all(count == 1 for count in calls.values())


# --------------------------------------------------------------------------- #
# 5. Yield-curve spread only when BOTH legs are present
# --------------------------------------------------------------------------- #


def test_get_market_regime_yield_curve_spread_needs_both_legs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    market._market_regime_cache.clear()
    _install_fake_tickers(monkeypatch, _all_regime_histories())

    result = market.market_data_service.get_market_regime()
    assert result["yield_curve_10y_3m_spread_pct"] == pytest.approx(4.65 - 3.745)

    market._market_regime_cache.clear()
    _install_fake_tickers(
        monkeypatch, _all_regime_histories(), raises=frozenset({"^IRX"})
    )
    result = market.market_data_service.get_market_regime()
    assert result["treasury_10y_yield_pct"] == pytest.approx(4.65)
    assert result["treasury_3m_yield_pct"] is None
    assert result["yield_curve_10y_3m_spread_pct"] is None


# --------------------------------------------------------------------------- #
# 6. macro_news prompt carries the market_regime block
# --------------------------------------------------------------------------- #


def test_macro_news_prompt_contains_market_regime() -> None:
    ctx = AgentContext(
        symbol=_symbol("AAPL"),
        is_favorite=False,
        price_summary={"close": 100.0},
        indicators={},
        fundamentals={},
        macro_news=[{"source": "Federal Reserve", "title": "Rate decision"}],
        corporate_news=[],
        risk_profile="prudente",
        market_regime={"vix_level": 16.91, "credit_hyg_lqd_ratio_change_30d_pct": -1.2},
    )

    prompt = MacroNewsAnalystAgent().build_user_prompt(ctx)

    assert "market_regime" in prompt
    payload = json.loads(prompt.split("\n", 1)[1])
    assert payload["market_regime"]["vix_level"] == pytest.approx(16.91)


# --------------------------------------------------------------------------- #
# 7. _build_risk_metrics propagates the regime fields
# --------------------------------------------------------------------------- #


def test_build_risk_metrics_propagates_regime_fields() -> None:
    metrics = MarketMetrics(
        last_close=100.0,
        atr14=2.0,
        sma50=95.0,
        sma200=90.0,
        rsi14=55.0,
        drawdown_90d_pct=-5.0,
        volatility_30d_pct=1.5,
    )
    market_regime = {
        "vix_level": 16.91,
        "vix_change_30d_pct": -15.45,
        "credit_hyg_lqd_ratio_change_30d_pct": -1.2,
        "eurusd_level": 1.141,  # not surfaced in risk_metrics; must be ignored
    }

    risk_metrics = orchestrator._build_risk_metrics(metrics, {}, {}, market_regime, [])

    assert risk_metrics["vix_level"] == pytest.approx(16.91)
    assert risk_metrics["vix_change_30d_pct"] == pytest.approx(-15.45)
    assert risk_metrics["credit_hyg_lqd_ratio_change_30d_pct"] == pytest.approx(-1.2)
    assert "eurusd_level" not in risk_metrics
