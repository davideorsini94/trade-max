"""Offline tests for the price-history endpoint's extended chart ranges
(5 anni / 10 anni / sempre). ``MarketDataService`` is monkeypatched at the
point where ``app.api.prices`` imports it, so refreshes never touch the
network (mirrors ``test_overview.py``'s pattern).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

import app.api.prices as prices_module
from app.models import PriceHistory, Symbol


class FakeMarketDataService:
    """Records every refresh_prices call instead of hitting yfinance."""

    calls: list[tuple[str, int]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def refresh_prices(
        self, db: Session, symbol: Symbol, interval: str = "1d", days: int = 730
    ) -> int:
        FakeMarketDataService.calls.append((interval, days))
        return 0


@pytest.fixture(autouse=True)
def _patch_market_data_service(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeMarketDataService.calls = []
    monkeypatch.setattr(prices_module, "MarketDataService", FakeMarketDataService)


def _seed_symbol_with_history(
    factory: sessionmaker[Session], ticker: str, *, n_days: int
) -> int:
    """A symbol with ``n_days`` of FRESH daily history ending today."""
    session = factory()
    try:
        symbol = Symbol(ticker=ticker, name=f"{ticker} Inc.", exchange="NASDAQ", currency="USD")
        session.add(symbol)
        session.commit()
        session.refresh(symbol)
        symbol_id = symbol.id

        now = datetime.utcnow()
        for i in range(n_days):
            close = 100.0 + i
            session.add(
                PriceHistory(
                    symbol_id=symbol_id,
                    ts=now - timedelta(days=(n_days - 1 - i)),
                    interval="1d",
                    open=close - 1.0,
                    high=close + 2.0,
                    low=close - 3.0,
                    close=close,
                    volume=1_000_000.0,
                )
            )
        session.commit()
        return symbol_id
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# 1. The endpoint accepts ranges well beyond the old 730-day cap
# --------------------------------------------------------------------------- #


def test_endpoint_accepts_ten_year_range(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    symbol_id = _seed_symbol_with_history(db_session_factory, "TENY", n_days=60)
    resp = client.get(f"/api/symbols/{symbol_id}/prices", params={"days": 3650})
    assert resp.status_code == 200


def test_endpoint_accepts_all_time_range(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    symbol_id = _seed_symbol_with_history(db_session_factory, "MAXY", n_days=60)
    resp = client.get(
        f"/api/symbols/{symbol_id}/prices", params={"days": prices_module.ALL_TIME_DAYS}
    )
    assert resp.status_code == 200


def test_endpoint_rejects_range_beyond_all_time_sentinel(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    symbol_id = _seed_symbol_with_history(db_session_factory, "OVER", n_days=10)
    resp = client.get(
        f"/api/symbols/{symbol_id}/prices",
        params={"days": prices_module.ALL_TIME_DAYS + 1},
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# 2. Requesting a long range triggers a DEEP refresh even when the latest bar
#    is already fresh (the pre-existing staleness check alone would miss this)
# --------------------------------------------------------------------------- #


def test_long_range_triggers_deep_refresh_even_when_fresh(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    # Only 60 days stored, but all of it is fresh (ends today) -> the OLD
    # staleness-only check would never refresh, leaving "10 anni" truncated.
    symbol_id = _seed_symbol_with_history(db_session_factory, "DEEP", n_days=60)

    client.get(f"/api/symbols/{symbol_id}/prices", params={"days": 3650})

    assert FakeMarketDataService.calls == [("1d", 3650)]  # max(3650, 730) = 3650


def test_short_range_with_fresh_full_history_does_not_refresh(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    # 800 days of fresh history already covers the requested 180-day window
    # with plenty of depth to spare -> no refresh needed at all.
    symbol_id = _seed_symbol_with_history(db_session_factory, "FRESH", n_days=800)

    client.get(f"/api/symbols/{symbol_id}/prices", params={"days": 180})

    assert FakeMarketDataService.calls == []


def test_refresh_days_is_at_least_730_for_short_ranges(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    # No history at all -> stale (last_ts is None) -> refresh triggers; even
    # for a short requested range the refresh still fetches the routine 730d
    # floor (unchanged behavior for the common case).
    symbol_id = _seed_symbol_with_history(db_session_factory, "EMPTY", n_days=0)

    client.get(f"/api/symbols/{symbol_id}/prices", params={"days": 30})

    assert FakeMarketDataService.calls == [("1d", 730)]
