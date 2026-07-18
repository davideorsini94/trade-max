"""API tests for symbol management and settings (offline, no network calls).

``MarketDataService`` is monkeypatched at the point where ``app.api.symbols``
imports it, so ``POST /api/symbols`` never touches yfinance/Yahoo: the fake
service returns a fixed ticker profile and seeds a couple of synthetic daily
bars, exactly like the real ``validate_and_profile`` / ``refresh_prices``
would for a valid ticker.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


class FakeMarketDataService:
    """Offline stand-in for ``app.data.market.MarketDataService``.

    ``NOPE`` simulates a ticker that does not exist (``validate_and_profile``
    returns ``None`` -> 404); any other ticker resolves to a fake equity
    profile.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def validate_and_profile(self, ticker: str) -> dict | None:
        if ticker.upper() == "NOPE":
            return None
        return {
            "ticker": ticker.upper(),
            "name": f"{ticker.upper()} Inc.",
            "exchange": "NASDAQ",
            "currency": "USD",
            "asset_type": "EQUITY",
        }

    def refresh_prices(self, db: Session, symbol) -> None:  # noqa: ANN001
        from app.models import PriceHistory

        now = datetime.utcnow()
        db.add(
            PriceHistory(
                symbol_id=symbol.id,
                ts=now - timedelta(days=1),
                interval="1d",
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1_000_000.0,
            )
        )
        db.add(
            PriceHistory(
                symbol_id=symbol.id,
                ts=now,
                interval="1d",
                open=100.0,
                high=103.0,
                low=100.0,
                close=102.0,
                volume=1_200_000.0,
            )
        )
        db.commit()

    def search(self, query: str) -> list[dict]:
        return []


@pytest.fixture(autouse=True)
def _patch_market_data_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.api.symbols.MarketDataService", FakeMarketDataService)


def test_add_symbol_creates_and_seeds_history(client: TestClient) -> None:
    resp = client.post("/api/symbols", json={"ticker": "aapl"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["ticker"] == "AAPL"
    assert body["name"] == "AAPL Inc."
    assert body["exchange"] == "NASDAQ"
    assert body["currency"] == "USD"
    assert body["is_favorite"] is False
    assert body["is_active"] is True

    # History was seeded by the fake refresh_prices -> quotes are available.
    listing = client.get("/api/symbols", params={"with_quotes": True}).json()
    aapl = next(item for item in listing if item["ticker"] == "AAPL")
    assert aapl["last_price"] == 102.0
    assert aapl["change_pct_1d"] == pytest.approx(2.0)


def test_add_symbol_not_found_returns_404(client: TestClient) -> None:
    resp = client.post("/api/symbols", json={"ticker": "NOPE"})
    assert resp.status_code == 404
    assert "non trovato" in resp.json()["detail"]


def test_add_symbol_duplicate_returns_409(client: TestClient) -> None:
    first = client.post("/api/symbols", json={"ticker": "MSFT"})
    assert first.status_code == 201

    duplicate = client.post("/api/symbols", json={"ticker": "msft"})
    assert duplicate.status_code == 409
    assert "già monitorato" in duplicate.json()["detail"]


def test_favorite_toggle(client: TestClient) -> None:
    created = client.post("/api/symbols", json={"ticker": "GOOG"}).json()
    symbol_id = created["id"]

    resp_on = client.post(f"/api/symbols/{symbol_id}/favorite", json={"is_favorite": True})
    assert resp_on.status_code == 200
    assert resp_on.json()["is_favorite"] is True

    resp_off = client.post(f"/api/symbols/{symbol_id}/favorite", json={"is_favorite": False})
    assert resp_off.status_code == 200
    assert resp_off.json()["is_favorite"] is False


def test_favorite_toggle_missing_symbol_returns_404(client: TestClient) -> None:
    resp = client.post("/api/symbols/999999/favorite", json={"is_favorite": True})
    assert resp.status_code == 404
    assert "non trovato" in resp.json()["detail"]


def test_list_ordering_favorites_first(client: TestClient) -> None:
    client.post("/api/symbols", json={"ticker": "AAA"})
    client.post("/api/symbols", json={"ticker": "BBB"})
    created_c = client.post("/api/symbols", json={"ticker": "CCC"}).json()

    client.post(f"/api/symbols/{created_c['id']}/favorite", json={"is_favorite": True})

    listing = client.get("/api/symbols").json()
    tickers = [item["ticker"] for item in listing]

    assert tickers[0] == "CCC"
    assert set(tickers[1:]) == {"AAA", "BBB"}


def test_list_favorites_only_filter(client: TestClient) -> None:
    client.post("/api/symbols", json={"ticker": "DDD"})
    created_e = client.post("/api/symbols", json={"ticker": "EEE"}).json()
    client.post(f"/api/symbols/{created_e['id']}/favorite", json={"is_favorite": True})

    listing = client.get("/api/symbols", params={"favorites_only": True}).json()
    tickers = [item["ticker"] for item in listing]

    assert tickers == ["EEE"]


def test_delete_symbol(client: TestClient) -> None:
    created = client.post("/api/symbols", json={"ticker": "DEL"}).json()
    symbol_id = created["id"]

    resp = client.delete(f"/api/symbols/{symbol_id}")
    assert resp.status_code == 204

    listing = client.get("/api/symbols").json()
    assert all(item["id"] != symbol_id for item in listing)

    resp_again = client.delete(f"/api/symbols/{symbol_id}")
    assert resp_again.status_code == 404


def test_settings_get_put_roundtrip(client: TestClient) -> None:
    initial = client.get("/api/settings")
    assert initial.status_code == 200
    initial_body = initial.json()
    assert initial_body["risk_profile"] == "prudente"
    assert initial_body["total_budget"] == 10000.0

    updated = client.put(
        "/api/settings",
        json={"total_budget": 20000, "risk_profile": "dinamico", "cash_reserve_pct": 15},
    )
    assert updated.status_code == 200
    updated_body = updated.json()
    assert updated_body["total_budget"] == 20000
    assert updated_body["risk_profile"] == "dinamico"
    assert updated_body["cash_reserve_pct"] == 15

    reread = client.get("/api/settings").json()
    assert reread["total_budget"] == 20000
    assert reread["risk_profile"] == "dinamico"
    assert reread["cash_reserve_pct"] == 15
