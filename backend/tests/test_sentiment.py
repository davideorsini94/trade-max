"""Offline tests for the market-sentiment analyst and its data/orchestration wiring.

Everything that would touch yfinance is monkeypatched (``app.data.market.yf.Ticker``),
so these tests never hit the network. Cache dicts are cleared explicitly where a
test exercises the failure/self-heal path.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

import app.data.market as market
from app.agents import ANALYST_AGENTS
from app.agents.base import DEFAULT_DATA_QUALITY, AgentContext
from app.agents.sentiment import SentimentAnalystAgent
from app.schemas import SymbolOut

# Exact deterministic Italian summary the orchestrator uses when a symbol has no
# sentiment data (kept in sync with orchestrator._DET_SENTIMENT).
_EXPECTED_SENTIMENT_SUMMARY_IT = (
    "Nessun dato disponibile su consenso degli analisti, operazioni degli insider "
    "o investitori istituzionali per questo titolo dalle fonti monitorate."
)


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
# validate_output
# --------------------------------------------------------------------------- #


def test_validate_output_defaults_on_empty_input() -> None:
    out = SentimentAnalystAgent().validate_output({})
    assert out["stance"] == "NEUTRAL"
    assert out["signal"] == 0.0
    assert out["confidence"] == 0.0
    assert out["consensus"] == "UNKNOWN"
    assert out["key_points"] == []
    assert out["risks"] == []
    assert out["data_quality"] == DEFAULT_DATA_QUALITY  # falls back to the shared default
    assert out["summary_it"] == ""


def test_validate_output_clamps_and_coerces() -> None:
    out = SentimentAnalystAgent().validate_output(
        {
            "signal": 3,  # -> clamped to 1.0
            "confidence": -2,  # -> clamped to 0.0
            "consensus": "buy",  # -> upper-cased to BUY
            "key_points": [f"p{i}" for i in range(7)],  # -> truncated to 5
            "risks": [f"r{i}" for i in range(5)],  # -> truncated to 3
        }
    )
    assert out["signal"] == 1.0
    assert out["confidence"] == 0.0
    assert out["consensus"] == "BUY"
    assert len(out["key_points"]) == 5
    assert len(out["risks"]) == 3

    # An unrecognized consensus falls back to UNKNOWN.
    garbage = SentimentAnalystAgent().validate_output({"consensus": "garbage"})
    assert garbage["consensus"] == "UNKNOWN"


# --------------------------------------------------------------------------- #
# build_user_prompt
# --------------------------------------------------------------------------- #


def test_user_prompt_payload_contains_recommendation_mean_and_ticker() -> None:
    ctx = AgentContext(
        symbol=_symbol("MSFT"),
        is_favorite=False,
        price_summary={"close": 100.0},
        indicators={},
        fundamentals={},
        macro_news=[],
        corporate_news=[],
        risk_profile="prudente",
        sentiment={"recommendation_mean": 2.1, "analyst_count": 30.0},
    )
    prompt = SentimentAnalystAgent().build_user_prompt(ctx)
    assert "recommendation_mean" in prompt
    assert "MSFT" in prompt
    # The JSON portion is valid and carries the sentiment slice.
    payload = json.loads(prompt.split("\n", 1)[1])
    assert payload["sentiment"]["recommendation_mean"] == 2.1
    assert payload["symbol"]["ticker"] == "MSFT"


# --------------------------------------------------------------------------- #
# Deterministic skip wiring (orchestrator)
# --------------------------------------------------------------------------- #


def test_deterministic_skip_includes_sentiment_when_empty() -> None:
    from app.engine.orchestrator import _deterministic_analyst_outputs

    data = {
        "macro_news": [{"title": "Fed holds rates"}],
        "corporate_news": [{"title": "Q3 earnings beat"}],
        "fundamentals": {"pe": 20.0, "forward_pe": None, "eps": None, "market_cap": None, "beta": None},
        "sentiment": market._empty_sentiment(),
    }
    skips = _deterministic_analyst_outputs(data)

    assert "sentiment" in skips
    sentiment = skips["sentiment"]
    assert sentiment["confidence"] == 0.2
    assert sentiment["data_quality"] == "POOR"
    assert sentiment["consensus"] == "UNKNOWN"
    assert sentiment["stance"] == "NEUTRAL"
    assert sentiment["signal"] == 0.0
    assert sentiment["key_points"] == []
    assert sentiment["risks"] == []
    assert sentiment["summary_it"] == _EXPECTED_SENTIMENT_SUMMARY_IT
    # The other analysts have data, so only sentiment is skipped here.
    assert "technical" not in skips


def test_no_sentiment_skip_when_recommendation_mean_present() -> None:
    from app.engine.orchestrator import _deterministic_analyst_outputs

    snapshot = market._empty_sentiment()
    snapshot["recommendation_mean"] = 2.0
    data = {
        "macro_news": [{"title": "x"}],
        "corporate_news": [{"title": "y"}],
        "fundamentals": {"pe": 20.0},
        "sentiment": snapshot,
    }
    assert "sentiment" not in _deterministic_analyst_outputs(data)


def test_min_failed_to_abort_is_one_less_than_analyst_count() -> None:
    from app.engine.orchestrator import _MIN_FAILED_ANALYSTS_TO_ABORT

    assert _MIN_FAILED_ANALYSTS_TO_ABORT == len(ANALYST_AGENTS) - 1
    assert _MIN_FAILED_ANALYSTS_TO_ABORT == 4  # current registry has 5 analysts


# --------------------------------------------------------------------------- #
# MarketDataService.get_sentiment_snapshot / get_relative_performance
# --------------------------------------------------------------------------- #


def test_get_sentiment_snapshot_degrades_when_ticker_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    market._sentiment_cache.clear()

    def _boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("yfinance down")

    monkeypatch.setattr(market.yf, "Ticker", _boom)

    result = market.market_data_service.get_sentiment_snapshot("AAPL")

    # Full template returned, nothing raised, and a failed result is NOT cached.
    assert result == market._empty_sentiment()
    assert result["recommendation_mean"] is None
    assert result["ratings_trend"] == []
    assert result["insider_transactions"] == []
    assert "AAPL" not in market._sentiment_cache


def test_get_relative_performance_degrades_when_history_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    market._benchmark_cache.clear()

    def _boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("yfinance down")

    monkeypatch.setattr(market.yf, "Ticker", _boom)

    result = market.market_data_service.get_relative_performance("ENI.MI", None, None)

    assert result["benchmark"] == "FTSEMIB.MI"  # benchmark still resolved
    assert result["benchmark_change_30d_pct"] is None
    assert result["benchmark_change_90d_pct"] is None
    assert result["relative_30d_pct"] is None
    assert result["relative_90d_pct"] is None
    assert result["stock_change_30d_pct"] is None
    assert result["stock_change_90d_pct"] is None
    # Retro-compatible: with no sector supplied, every sector-specific key is None.
    assert result["sector"] is None
    assert result["sector_etf"] is None
    assert result["sector_change_30d_pct"] is None
    assert result["sector_change_90d_pct"] is None
    assert result["relative_sector_30d_pct"] is None
    assert result["relative_sector_90d_pct"] is None


# --------------------------------------------------------------------------- #
# Sector relative strength (SPDR sector ETF proxy)
# --------------------------------------------------------------------------- #


def test_sector_etf_for_maps_names_aliases_and_unknown() -> None:
    svc = market.market_data_service
    assert svc.sector_etf_for("Technology") == "XLK"
    assert svc.sector_etf_for("Consumer Discretionary") == "XLY"  # GICS alias
    assert svc.sector_etf_for(" Technology ") == "XLK"  # trimmed before lookup
    assert svc.sector_etf_for(None) is None
    assert svc.sector_etf_for("Boh") is None  # unmapped sector


def test_get_relative_performance_computes_sector_spread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # _benchmark_changes is reused for both the main benchmark and the sector ETF;
    # return distinct values per ticker so we can assert both spreads independently.
    def _fake_benchmark_changes(
        self: market.MarketDataService, benchmark: str
    ) -> tuple[float | None, float | None]:
        if benchmark == "XLK":  # Technology sector ETF
            return (2.0, 5.0)
        return (1.0, 3.0)  # main benchmark (^GSPC for AAPL)

    monkeypatch.setattr(
        market.MarketDataService, "_benchmark_changes", _fake_benchmark_changes
    )

    result = market.market_data_service.get_relative_performance(
        "AAPL", 10.0, 12.0, sector="Technology"
    )

    # Sector proxy resolved and spreads computed as (stock - sector).
    assert result["sector"] == "Technology"
    assert result["sector_etf"] == "XLK"
    assert result["sector_change_30d_pct"] == 2.0
    assert result["sector_change_90d_pct"] == 5.0
    assert result["relative_sector_30d_pct"] == pytest.approx(8.0)  # 10 - 2
    assert result["relative_sector_90d_pct"] == pytest.approx(7.0)  # 12 - 5
    # Main-benchmark spreads unaffected and still computed against ^GSPC.
    assert result["benchmark"] == "^GSPC"
    assert result["benchmark_change_30d_pct"] == 1.0
    assert result["relative_30d_pct"] == pytest.approx(9.0)  # 10 - 1
    assert result["relative_90d_pct"] == pytest.approx(9.0)  # 12 - 3


def test_get_relative_performance_unmapped_sector_keeps_benchmark_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_benchmark_changes(
        self: market.MarketDataService, benchmark: str
    ) -> tuple[float | None, float | None]:
        return (1.0, 3.0)

    monkeypatch.setattr(
        market.MarketDataService, "_benchmark_changes", _fake_benchmark_changes
    )

    result = market.market_data_service.get_relative_performance(
        "AAPL", 10.0, 12.0, sector="Boh"
    )

    # Unmapped sector: only the "sector" echo survives; the rest stay None.
    assert result["sector"] == "Boh"
    assert result["sector_etf"] is None
    assert result["sector_change_30d_pct"] is None
    assert result["sector_change_90d_pct"] is None
    assert result["relative_sector_30d_pct"] is None
    assert result["relative_sector_90d_pct"] is None
    # Benchmark fields remain intact.
    assert result["benchmark"] == "^GSPC"
    assert result["benchmark_change_30d_pct"] == 1.0
    assert result["benchmark_change_90d_pct"] == 3.0
    assert result["relative_30d_pct"] == pytest.approx(9.0)  # 10 - 1
    assert result["relative_90d_pct"] == pytest.approx(9.0)  # 12 - 3


# --------------------------------------------------------------------------- #
# benchmark_for
# --------------------------------------------------------------------------- #


def test_benchmark_for_maps_suffix_and_default() -> None:
    svc = market.market_data_service
    assert svc.benchmark_for("ENI.MI") == "FTSEMIB.MI"  # Italian exchange suffix
    assert svc.benchmark_for("AAPL") == "^GSPC"  # unsuffixed US ticker -> S&P 500 default
