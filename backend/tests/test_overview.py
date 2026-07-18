"""API tests for the symbol overview endpoint (offline, no network calls).

``MarketDataService`` is monkeypatched at the point where ``app.api.symbols``
imports it, so both the stale-price refresh and the fundamentals pull are fully
offline: seeded daily bars are always fresh (the fake ``refresh_prices`` is a
no-op) and ``get_fundamentals`` returns a fixed snapshot. The module-level
fundamentals cache is cleared before each test so snapshots never leak.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.models import PriceHistory, Symbol

# Fixed fundamentals snapshot returned by the fake service. Keys mirror
# ``MarketDataService.get_fundamentals`` exactly (see app/data/market.py).
FAKE_FUNDAMENTALS: dict[str, Any] = {
    "pe": 28.5,
    "forward_pe": 25.0,
    "eps": 6.13,
    "market_cap": 3.4e12,
    "dividend_yield": 0.0044,
    "beta": 1.28,
    "margins": 0.25,
    "revenue_growth": 0.08,
    "debt_to_equity": 1.5,
    "analyst_target": 260.0,
    "sector": "Technology",
    "industry": "Consumer Electronics",
}


class FakeMarketDataService:
    """Offline stand-in: no-op price refresh + fixed fundamentals snapshot."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def refresh_prices(
        self, db: Session, symbol: Symbol, interval: str = "1d", days: int = 730
    ) -> int:
        return 0

    def get_fundamentals(self, ticker: str) -> dict:
        return dict(FAKE_FUNDAMENTALS)


@pytest.fixture(autouse=True)
def _patch_market_data_service(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.api.symbols as symbols_module

    monkeypatch.setattr(symbols_module, "MarketDataService", FakeMarketDataService)
    symbols_module._fundamentals_cache.clear()


def _seed_symbol(
    factory: sessionmaker[Session],
    ticker: str,
    *,
    n_rows: int,
) -> int:
    """Insert a symbol plus ``n_rows`` daily bars, newest last (ts = today).

    Row ``i`` (0-based) has: close = 100 + i, open = close - 1, high = close + 2,
    low = close - 3, volume = 1_000_000 + i * 1_000. The latest bar is dated
    ``utcnow()`` so the overview never considers the history stale.
    """
    session = factory()
    try:
        symbol = Symbol(ticker=ticker, name=f"{ticker} Inc.", exchange="NASDAQ", currency="USD")
        session.add(symbol)
        session.commit()
        session.refresh(symbol)
        symbol_id = symbol.id

        now = datetime.utcnow()
        for i in range(n_rows):
            close = 100.0 + i
            session.add(
                PriceHistory(
                    symbol_id=symbol_id,
                    ts=now - timedelta(days=(n_rows - 1 - i)),
                    interval="1d",
                    open=close - 1.0,
                    high=close + 2.0,
                    low=close - 3.0,
                    close=close,
                    volume=1_000_000.0 + i * 1_000.0,
                )
            )
        session.commit()
        return symbol_id
    finally:
        session.close()


def test_overview_computes_all_fields(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    symbol_id = _seed_symbol(db_session_factory, "OVW1", n_rows=60)

    resp = client.get(f"/api/symbols/{symbol_id}/overview")
    assert resp.status_code == 200
    body = resp.json()

    # Static symbol fields.
    assert body["ticker"] == "OVW1"
    assert body["name"] == "OVW1 Inc."
    assert body["exchange"] == "NASDAQ"
    assert body["currency"] == "USD"
    assert body["asset_type"] == "EQUITY"

    # Latest bar (i = 59): close 159, open 158, high 161, low 156, vol 1_059_000.
    assert body["last_price"] == pytest.approx(159.0)
    assert body["open"] == pytest.approx(158.0)
    assert body["day_high"] == pytest.approx(161.0)
    assert body["day_low"] == pytest.approx(156.0)
    assert body["volume"] == pytest.approx(1_059_000.0)
    assert body["updated_at"] is not None

    # Previous close (i = 58) and change math.
    assert body["prev_close"] == pytest.approx(158.0)
    assert body["change_1d_abs"] == pytest.approx(1.0)
    assert body["change_pct_1d"] == pytest.approx(1.0 / 158.0 * 100.0)

    # Avg volume over last 30 rows (i = 30..59) -> mean = 1_044_500.
    assert body["avg_volume_30d"] == pytest.approx(1_044_500.0)

    # 52-week range over the whole window: high of last row, low of first row.
    assert body["week52_high"] == pytest.approx(161.0)  # i = 59: close + 2
    assert body["week52_low"] == pytest.approx(97.0)  # i = 0: close - 3

    # Fundamentals mapped from the fake snapshot's exact keys.
    assert body["market_cap"] == pytest.approx(3.4e12)
    assert body["pe"] == pytest.approx(28.5)
    assert body["forward_pe"] == pytest.approx(25.0)
    assert body["eps"] == pytest.approx(6.13)
    assert body["dividend_yield"] == pytest.approx(0.0044)
    assert body["beta"] == pytest.approx(1.28)
    assert body["analyst_target"] == pytest.approx(260.0)
    assert body["sector"] == "Technology"
    assert body["industry"] == "Consumer Electronics"


def test_overview_missing_symbol_returns_404(client: TestClient) -> None:
    resp = client.get("/api/symbols/999999/overview")
    assert resp.status_code == 404
    assert "non trovato" in resp.json()["detail"]


def test_overview_single_row_history_is_none_safe(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    # One bar (i = 0): close 100, open 99, high 102, low 97, vol 1_000_000.
    symbol_id = _seed_symbol(db_session_factory, "OVW2", n_rows=1)

    resp = client.get(f"/api/symbols/{symbol_id}/overview")
    assert resp.status_code == 200
    body = resp.json()

    assert body["last_price"] == pytest.approx(100.0)
    assert body["open"] == pytest.approx(99.0)
    assert body["day_high"] == pytest.approx(102.0)
    assert body["day_low"] == pytest.approx(97.0)
    assert body["volume"] == pytest.approx(1_000_000.0)
    assert body["updated_at"] is not None

    # No previous bar -> change fields stay None; single-row 52w range/avg exist.
    assert body["prev_close"] is None
    assert body["change_1d_abs"] is None
    assert body["change_pct_1d"] is None
    assert body["avg_volume_30d"] == pytest.approx(1_000_000.0)
    assert body["week52_high"] == pytest.approx(102.0)
    assert body["week52_low"] == pytest.approx(97.0)

    # Fundamentals are still populated even with a one-row price history.
    assert body["market_cap"] == pytest.approx(3.4e12)
    assert body["sector"] == "Technology"


def test_overview_empty_history_all_price_fields_none(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    symbol_id = _seed_symbol(db_session_factory, "OVW3", n_rows=0)

    resp = client.get(f"/api/symbols/{symbol_id}/overview")
    assert resp.status_code == 200
    body = resp.json()

    for field in (
        "last_price",
        "open",
        "day_high",
        "day_low",
        "volume",
        "updated_at",
        "prev_close",
        "change_1d_abs",
        "change_pct_1d",
        "avg_volume_30d",
        "week52_high",
        "week52_low",
    ):
        assert body[field] is None, f"expected {field} to be None with empty history"

    # Static + fundamentals fields remain populated regardless of price history.
    assert body["ticker"] == "OVW3"
    assert body["pe"] == pytest.approx(28.5)
    assert body["industry"] == "Consumer Electronics"
