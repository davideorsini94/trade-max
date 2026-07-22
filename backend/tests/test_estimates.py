"""Offline tests for analyst forward estimates and their data/orchestration wiring.

Everything that would touch yfinance is monkeypatched (``app.data.market.yf.Ticker``),
so these tests never hit the network. The DataFrames are shaped exactly like the
real yfinance 0.2.66 ``earningsTrend`` output (index by ``period`` with rows
``"0q"/"+1q"/"0y"/"+1y"``), including the incoherent ``downLast7Days`` casing in
``eps_revisions``. Cache dicts are cleared where a test exercises the fetch path.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime

import pandas as pd
import pytest

import app.data.market as market
import app.engine.orchestrator as orchestrator
from app.agents.base import AgentContext
from app.agents.fundamentals import FundamentalsAnalystAgent
from app.schemas import SymbolOut

_PERIOD_INDEX = pd.Index(["0q", "+1q", "0y", "+1y"], name="period")


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


# --------------------------------------------------------------------------- #
# Fake yfinance Ticker + DataFrame builders (shaped like real 0.2.66 output)
# --------------------------------------------------------------------------- #


def _eps_trend_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "current": [1.89396, 2.01790, 8.76760, 9.71110],
            "7daysAgo": [1.89428, 2.01204, 8.76215, 9.68640],
            "30daysAgo": [1.89429, 2.00836, 8.75958, 9.67425],
            "60daysAgo": [1.89429, 2.00767, 8.75324, 9.65098],
            "90daysAgo": [1.73522, 1.97264, 8.50388, 9.38071],
        },
        index=_PERIOD_INDEX,
    )


def _earnings_estimate_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "avg": [1.89396, 2.01790, 8.76760, 9.71110],
            "low": [1.83, 1.88, 8.29, 8.81],
            "high": [1.99, 2.15, 9.04, 10.96],
            "yearAgoEps": [1.57, 1.85, 7.46, 8.7676],
            "numberOfAnalysts": [31, 29, 41, 43],
            "growth": [0.2063, 0.0908, 0.1753, 0.1076],
        },
        index=_PERIOD_INDEX,
    )


def _revenue_estimate_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "avg": [108890141050, 114801264110, 478714605930, 522949683990],
            "low": [107501000000, 106178615430, 469821092640, 483566000000],
            "high": [111000000000, 120000000000, 490000000000, 540000000000],
            "numberOfAnalysts": [24, 22, 41, 40],
            "yearAgoRevenue": [94036000000, 102466000000, 416161000000, 478714605930],
            "growth": [0.1580, 0.1204, 0.1503, 0.0924],
        },
        index=_PERIOD_INDEX,
    )


def _eps_revisions_df() -> pd.DataFrame:
    # NOTE the incoherent yfinance casing: downLast7Days has a capital D.
    return pd.DataFrame(
        {
            "upLast7days": [0, 1, 2, 2],
            "upLast30days": [24, 2, 4, 8],
            "downLast30days": [0, 0, 0, 1],
            "downLast7Days": [0, 0, 1, 1],
        },
        index=_PERIOD_INDEX,
    )


class _FakeTicker:
    """Stands in for ``yf.Ticker``; each property returns a preset value or raises."""

    def __init__(
        self,
        *,
        eps_trend: object = None,
        earnings_estimate: object = None,
        revenue_estimate: object = None,
        eps_revisions: object = None,
        raises: frozenset[str] = frozenset(),
    ) -> None:
        self._values = {
            "eps_trend": eps_trend,
            "earnings_estimate": earnings_estimate,
            "revenue_estimate": revenue_estimate,
            "eps_revisions": eps_revisions,
        }
        self._raises = raises

    def _read(self, name: str) -> object:
        if name in self._raises:
            raise RuntimeError(f"{name} boom")
        return self._values[name]

    @property
    def eps_trend(self) -> object:
        return self._read("eps_trend")

    @property
    def earnings_estimate(self) -> object:
        return self._read("earnings_estimate")

    @property
    def revenue_estimate(self) -> object:
        return self._read("revenue_estimate")

    @property
    def eps_revisions(self) -> object:
        return self._read("eps_revisions")


# --------------------------------------------------------------------------- #
# 1. Full, well-formed DataFrames -> shape + normalization
# --------------------------------------------------------------------------- #


def test_get_analyst_estimates_full_shape_and_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    market._estimates_cache.clear()
    monkeypatch.setattr(
        market.yf,
        "Ticker",
        lambda *a, **k: _FakeTicker(
            eps_trend=_eps_trend_df(),
            earnings_estimate=_earnings_estimate_df(),
            revenue_estimate=_revenue_estimate_df(),
            eps_revisions=_eps_revisions_df(),
        ),
    )

    result = market.market_data_service.get_analyst_estimates("AAPL")

    # Only the two annual rows are read; quarterly rows are ignored.
    cy = result["eps_current_year"]
    assert cy["avg"] == pytest.approx(8.76760)
    assert cy["n_analysts"] == 41
    assert isinstance(cy["n_analysts"], int)
    assert cy["growth_pct"] == pytest.approx(17.53)  # 0.1753 * 100
    assert cy["revision_7d_pct"] == pytest.approx((8.76760 - 8.76215) / 8.76215 * 100)
    assert cy["revision_30d_pct"] == pytest.approx((8.76760 - 8.75958) / 8.75958 * 100)
    assert cy["revision_90d_pct"] == pytest.approx((8.76760 - 8.50388) / 8.50388 * 100)

    ny = result["eps_next_year"]
    assert ny["avg"] == pytest.approx(9.71110)
    assert ny["n_analysts"] == 43
    assert ny["growth_pct"] == pytest.approx(10.76)  # 0.1076 * 100

    assert result["revenue_growth_current_year_pct"] == pytest.approx(15.03)
    assert result["revenue_growth_next_year_pct"] == pytest.approx(9.24)

    # Summed across every present row: up = 24+2+4+8 = 38, down = 0+0+0+1 = 1.
    assert result["eps_revisions_up_30d"] == 38
    assert result["eps_revisions_down_30d"] == 1
    assert isinstance(result["eps_revisions_up_30d"], int)
    assert isinstance(result["eps_revisions_down_30d"], int)

    # A snapshot carrying data IS cached.
    assert "AAPL" in market._estimates_cache


def test_get_analyst_estimates_revision_none_when_denominator_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    market._estimates_cache.clear()
    trend = _eps_trend_df().copy()
    trend.loc["0y", "30daysAgo"] = 0.0  # zero denominator -> None (no ZeroDivisionError)
    trend.loc["0y", "7daysAgo"] = None  # missing value -> None
    monkeypatch.setattr(
        market.yf,
        "Ticker",
        lambda *a, **k: _FakeTicker(
            eps_trend=trend,
            earnings_estimate=_earnings_estimate_df(),
            revenue_estimate=_revenue_estimate_df(),
            eps_revisions=_eps_revisions_df(),
        ),
    )

    cy = market.market_data_service.get_analyst_estimates("AAPL")["eps_current_year"]
    assert cy["revision_7d_pct"] is None
    assert cy["revision_30d_pct"] is None
    assert cy["revision_90d_pct"] == pytest.approx((8.76760 - 8.50388) / 8.50388 * 100)


# --------------------------------------------------------------------------- #
# 2. Empty / missing DataFrames (non-US ticker) -> all None, NOT cached
# --------------------------------------------------------------------------- #


def test_get_analyst_estimates_empty_returns_all_none_and_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    market._estimates_cache.clear()
    calls = {"n": 0}

    def _factory(*a: object, **k: object) -> _FakeTicker:
        calls["n"] += 1
        return _FakeTicker(
            eps_trend=pd.DataFrame(),
            earnings_estimate=pd.DataFrame(),
            revenue_estimate=None,  # property may be missing entirely
            eps_revisions=pd.DataFrame(),
        )

    monkeypatch.setattr(market.yf, "Ticker", _factory)

    result = market.market_data_service.get_analyst_estimates("ENI.MI")

    assert result == market._empty_estimates()
    assert result["eps_current_year"] is None
    assert result["eps_next_year"] is None
    assert result["revenue_growth_current_year_pct"] is None
    assert result["eps_revisions_up_30d"] is None

    # A fully-empty snapshot is NOT cached, so a second call retries the fetch.
    assert "ENI.MI" not in market._estimates_cache
    market.market_data_service.get_analyst_estimates("ENI.MI")
    assert calls["n"] == 2


# --------------------------------------------------------------------------- #
# 3. One property raising must not blank the others
# --------------------------------------------------------------------------- #


def test_get_analyst_estimates_one_property_raising_keeps_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    market._estimates_cache.clear()
    monkeypatch.setattr(
        market.yf,
        "Ticker",
        lambda *a, **k: _FakeTicker(
            eps_trend=None,  # simulate the eps_trend read raising
            earnings_estimate=_earnings_estimate_df(),
            revenue_estimate=_revenue_estimate_df(),
            eps_revisions=_eps_revisions_df(),
            raises=frozenset({"eps_trend"}),
        ),
    )

    result = market.market_data_service.get_analyst_estimates("AAPL")

    cy = result["eps_current_year"]
    # earnings_estimate still populates avg/n_analysts/growth...
    assert cy["avg"] == pytest.approx(8.76760)
    assert cy["n_analysts"] == 41
    assert cy["growth_pct"] == pytest.approx(17.53)
    # ...but the revisions (from the failed eps_trend) are None, not a crash.
    assert cy["revision_7d_pct"] is None
    assert cy["revision_30d_pct"] is None
    assert cy["revision_90d_pct"] is None
    # The other DataFrames are unaffected.
    assert result["revenue_growth_current_year_pct"] == pytest.approx(15.03)
    assert result["eps_revisions_up_30d"] == 38
    assert result["eps_revisions_down_30d"] == 1


# --------------------------------------------------------------------------- #
# 4. FundamentalsAnalystAgent prompt carries the estimates block
# --------------------------------------------------------------------------- #


def test_fundamentals_prompt_contains_estimates_block() -> None:
    estimates = {
        "eps_current_year": {
            "avg": 8.77,
            "n_analysts": 41,
            "growth_pct": 17.53,
            "revision_7d_pct": 0.06,
            "revision_30d_pct": 0.09,
            "revision_90d_pct": 3.1,
        },
        "eps_next_year": None,
        "revenue_growth_current_year_pct": 15.03,
        "revenue_growth_next_year_pct": 9.24,
        "eps_revisions_up_30d": 38,
        "eps_revisions_down_30d": 1,
    }
    ctx = AgentContext(
        symbol=_symbol("AAPL"),
        is_favorite=False,
        price_summary={"close": 100.0},
        indicators={},
        fundamentals={"pe": 30.0, "estimates": estimates},
        macro_news=[],
        corporate_news=[],
        risk_profile="prudente",
    )

    prompt = FundamentalsAnalystAgent().build_user_prompt(ctx)

    assert "estimates" in prompt
    payload = json.loads(prompt.split("\n", 1)[1])
    assert payload["fundamentals"]["estimates"]["eps_current_year"]["n_analysts"] == 41
    assert payload["fundamentals"]["estimates"]["eps_revisions_up_30d"] == 38


# --------------------------------------------------------------------------- #
# 5. Orchestrator wiring: fundamentals["estimates"] is None when snapshot empty
# --------------------------------------------------------------------------- #


class _FakeDB:
    def get(self, model: object, ident: object) -> None:
        return None


@contextmanager
def _fake_session_scope():
    yield _FakeDB()


class _FakeMarket:
    """Minimal MarketDataService double for :func:`_gather_market_data`."""

    def __init__(self, estimates: dict) -> None:
        self._estimates = estimates

    def get_history_df(self, *a: object, **k: object) -> pd.DataFrame:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    def price_summary(self, *a: object, **k: object) -> dict:
        return {"close": 100.0, "change_pct_30d": 1.0, "change_pct_90d": 2.0}

    def get_fundamentals(self, *a: object, **k: object) -> dict:
        return {"pe": 20.0, "beta": 1.1, "sector": "Technology"}

    def get_analyst_estimates(self, *a: object, **k: object) -> dict:
        return self._estimates

    def get_sentiment_snapshot(self, *a: object, **k: object) -> dict:
        return market._empty_sentiment()

    def get_market_regime(self, *a: object, **k: object) -> dict:
        return market._empty_market_regime()

    def get_relative_performance(self, *a: object, **k: object) -> dict:
        return {}


class _FakeNews:
    def fetch_macro(self, *a: object, **k: object) -> list:
        return []

    def fetch_corporate(self, *a: object, **k: object) -> list:
        return []


def _run_gather(monkeypatch: pytest.MonkeyPatch, estimates: dict) -> dict:
    monkeypatch.setattr(orchestrator, "market_data_service", _FakeMarket(estimates))
    monkeypatch.setattr(orchestrator, "news_service", _FakeNews())
    monkeypatch.setattr(orchestrator, "session_scope", _fake_session_scope)
    monkeypatch.setattr(orchestrator, "compute_all", lambda df: {"latest": {}})
    return orchestrator._gather_market_data(symbol_id=1, ticker="AAPL")


def test_gather_market_data_estimates_none_when_snapshot_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = _run_gather(monkeypatch, market._empty_estimates())
    assert data["fundamentals"]["estimates"] is None


def test_gather_market_data_estimates_attached_when_snapshot_has_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    estimates = market._empty_estimates()
    estimates["eps_revisions_up_30d"] = 12
    data = _run_gather(monkeypatch, estimates)
    assert data["fundamentals"]["estimates"] == estimates
    assert data["fundamentals"]["estimates"]["eps_revisions_up_30d"] == 12
