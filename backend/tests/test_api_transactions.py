"""Offline API tests for the fictitious paper-trading transaction endpoints
(blueprint §5.4 addendum). ``MarketDataService`` is monkeypatched at the point
where ``app.api.transactions`` imports it, so a transaction dated before any
stored history never touches the network (mirrors ``test_overview.py``).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

import app.api.transactions as transactions_module
from app.models import PriceHistory, Symbol


class FakeMarketDataService:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def refresh_prices(self, db: Session, symbol: Symbol, interval: str = "1d", days: int = 730) -> int:
        return 0  # no network, no new rows


@pytest.fixture(autouse=True)
def _patch_market_data_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transactions_module, "MarketDataService", FakeMarketDataService)


def _seed_symbol_with_close(
    factory: sessionmaker[Session], ticker: str, *, close: float, ts: datetime, currency: str = "USD"
) -> int:
    # Real daily bars are always stored at (local-exchange) midnight, which is
    # what resolve_session_close's +12h normalization assumes; a raw wall-clock
    # timestamp (e.g. mid-afternoon) would roll over to the wrong session date.
    ts = datetime.combine(ts.date(), datetime.min.time())
    session = factory()
    try:
        symbol = Symbol(ticker=ticker, name=f"{ticker} Inc.", exchange="NASDAQ", currency=currency)
        session.add(symbol)
        session.commit()
        session.refresh(symbol)
        session.add(
            PriceHistory(
                symbol_id=symbol.id, ts=ts, interval="1d",
                open=close, high=close, low=close, close=close, volume=1_000_000.0,
            )
        )
        session.commit()
        return symbol.id
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# POST — happy path + validation
# --------------------------------------------------------------------------- #


def test_post_buy_happy_path_resolves_price_and_quantity(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    today = datetime.utcnow()
    symbol_id = _seed_symbol_with_close(db_session_factory, "TXB1", close=190.0, ts=today)

    resp = client.post(
        f"/api/symbols/{symbol_id}/transactions",
        json={"side": "BUY", "amount": 1000.0, "fee_pct": 1.0, "executed_at": today.date().isoformat()},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["price_ref"] == pytest.approx(190.0)
    assert body["quantity_est"] == pytest.approx(990.0 / 190.0)
    assert body["currency"] == "USD"


def test_post_accepts_backdated_transaction(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    old_date = datetime.utcnow() - timedelta(days=10)
    symbol_id = _seed_symbol_with_close(db_session_factory, "TXB2", close=100.0, ts=old_date)

    resp = client.post(
        f"/api/symbols/{symbol_id}/transactions",
        json={"side": "BUY", "amount": 500.0, "executed_at": old_date.date().isoformat()},
    )
    assert resp.status_code == 201


def test_post_future_date_rejected(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    symbol_id = _seed_symbol_with_close(db_session_factory, "TXB3", close=100.0, ts=datetime.utcnow())
    future = (datetime.utcnow() + timedelta(days=3)).date().isoformat()

    resp = client.post(
        f"/api/symbols/{symbol_id}/transactions",
        json={"side": "BUY", "amount": 500.0, "executed_at": future},
    )
    assert resp.status_code == 422


@pytest.mark.parametrize("fee_pct", [-1.0, 101.0])
def test_post_fee_out_of_range_rejected(
    client: TestClient, db_session_factory: sessionmaker[Session], fee_pct: float
) -> None:
    symbol_id = _seed_symbol_with_close(db_session_factory, "TXB4", close=100.0, ts=datetime.utcnow())

    resp = client.post(
        f"/api/symbols/{symbol_id}/transactions",
        json={
            "side": "BUY", "amount": 500.0, "fee_pct": fee_pct,
            "executed_at": datetime.utcnow().date().isoformat(),
        },
    )
    assert resp.status_code == 422


def test_post_zero_amount_rejected(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    symbol_id = _seed_symbol_with_close(db_session_factory, "TXB5", close=100.0, ts=datetime.utcnow())

    resp = client.post(
        f"/api/symbols/{symbol_id}/transactions",
        json={"side": "BUY", "amount": 0.0, "executed_at": datetime.utcnow().date().isoformat()},
    )
    assert resp.status_code == 422


def test_post_nonexistent_symbol_404(client: TestClient) -> None:
    resp = client.post(
        "/api/symbols/999999/transactions",
        json={"side": "BUY", "amount": 500.0, "executed_at": datetime.utcnow().date().isoformat()},
    )
    assert resp.status_code == 404


def test_post_sell_without_prior_buy_rejected(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    symbol_id = _seed_symbol_with_close(db_session_factory, "TXB6", close=100.0, ts=datetime.utcnow())

    resp = client.post(
        f"/api/symbols/{symbol_id}/transactions",
        json={"side": "SELL", "amount": 500.0, "executed_at": datetime.utcnow().date().isoformat()},
    )
    assert resp.status_code == 422


def test_post_sell_dated_before_first_buy_rejected(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    today = datetime.utcnow()
    symbol_id = _seed_symbol_with_close(db_session_factory, "TXB7", close=100.0, ts=today)
    client.post(
        f"/api/symbols/{symbol_id}/transactions",
        json={"side": "BUY", "amount": 500.0, "executed_at": today.date().isoformat()},
    )
    earlier = (today - timedelta(days=5)).date().isoformat()

    resp = client.post(
        f"/api/symbols/{symbol_id}/transactions",
        json={"side": "SELL", "amount": 500.0, "executed_at": earlier},
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# GET — ordering + position summary
# --------------------------------------------------------------------------- #


def test_get_returns_empty_position_with_no_transactions(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    symbol_id = _seed_symbol_with_close(db_session_factory, "TXG1", close=100.0, ts=datetime.utcnow())

    resp = client.get(f"/api/symbols/{symbol_id}/transactions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["position"] is None


def test_get_orders_newest_first_and_computes_position(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    today = datetime.utcnow()
    symbol_id = _seed_symbol_with_close(db_session_factory, "TXG2", close=200.0, ts=today)
    older = (today - timedelta(days=5)).date().isoformat()
    newer = today.date().isoformat()
    # A bar for the older date too, so BOTH transactions can resolve a quantity
    # (resolve_session_close only ever looks BACKWARD from a transaction's own
    # date, so a single "today" bar alone can't price a 5-day-old transaction).
    session = db_session_factory()
    try:
        session.add(
            PriceHistory(
                symbol_id=symbol_id,
                ts=datetime.combine((today - timedelta(days=5)).date(), datetime.min.time()),
                interval="1d", open=190.0, high=190.0, low=190.0, close=190.0, volume=1_000_000.0,
            )
        )
        session.commit()
    finally:
        session.close()

    client.post(
        f"/api/symbols/{symbol_id}/transactions",
        json={"side": "BUY", "amount": 500.0, "executed_at": older},
    )
    client.post(
        f"/api/symbols/{symbol_id}/transactions",
        json={"side": "BUY", "amount": 300.0, "executed_at": newer},
    )

    resp = client.get(f"/api/symbols/{symbol_id}/transactions")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["items"][0]["executed_at"] >= body["items"][1]["executed_at"]  # newest first
    assert body["position"]["status"] == "OPEN"
    assert body["position"]["invested_total"] == pytest.approx(800.0)
    assert body["position"]["last_close"] == pytest.approx(200.0)


# --------------------------------------------------------------------------- #
# DELETE
# --------------------------------------------------------------------------- #


def test_delete_removes_transaction_and_recomputes_position(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    today = datetime.utcnow()
    symbol_id = _seed_symbol_with_close(db_session_factory, "TXD1", close=150.0, ts=today)
    created = client.post(
        f"/api/symbols/{symbol_id}/transactions",
        json={"side": "BUY", "amount": 500.0, "executed_at": today.date().isoformat()},
    ).json()

    resp = client.delete(f"/api/transactions/{created['id']}")
    assert resp.status_code == 204

    after = client.get(f"/api/symbols/{symbol_id}/transactions").json()
    assert after["items"] == []
    assert after["position"] is None


def test_delete_nonexistent_transaction_404(client: TestClient) -> None:
    resp = client.delete("/api/transactions/999999")
    assert resp.status_code == 404
