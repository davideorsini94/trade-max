"""Offline tests for ``POST /api/universe/isin`` (ISIN lookup into the universe)."""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

import app.api.universe as universe_api
from app.models import Symbol, UniverseStat


class _FakeService:
    """Stand-in for MarketDataService with a canned search result."""

    def __init__(self, results):
        self._results = results

    def search(self, q: str):
        if isinstance(self._results, Exception):
            raise self._results
        return self._results


def _install_fake_search(monkeypatch: pytest.MonkeyPatch, results) -> None:
    monkeypatch.setattr(universe_api, "MarketDataService", lambda: _FakeService(results))


def _fake_refresh(db_session_factory: sessionmaker[Session]):
    """A refresh_single_ticker stand-in that persists a minimal stat row."""

    def _refresh(db: Session, ticker: str) -> UniverseStat | None:
        row = UniverseStat(
            ticker=ticker,
            name=f"{ticker} S.p.A.",
            country="IT",
            sector="Energy",
            currency="EUR",
            fame_rank=2,
            last_price=13.5,
            composite_score=40.0,
            updated_at=datetime.utcnow(),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    return _refresh


def test_isin_invalid_format_returns_400(client: TestClient) -> None:
    resp = client.post("/api/universe/isin", json={"isin": "NON-UN-ISIN"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "ISIN non valido."


def test_isin_no_results_returns_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_search(monkeypatch, [])
    resp = client.post("/api/universe/isin", json={"isin": "IT0000000009"})
    assert resp.status_code == 404
    assert "Nessun prodotto" in resp.json()["detail"]


def test_isin_provider_failure_returns_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_search(monkeypatch, RuntimeError("rete giù"))
    resp = client.post("/api/universe/isin", json={"isin": "IT0000000009"})
    assert resp.status_code == 502


def test_isin_happy_path_persists_and_returns_item(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: sessionmaker[Session],
) -> None:
    _install_fake_search(
        monkeypatch,
        [
            {"ticker": "ENI.MI", "name": "Eni", "asset_type": "EQUITY"},
            {"ticker": "E", "name": "Eni ADR", "asset_type": "EQUITY"},
        ],
    )
    monkeypatch.setattr(
        universe_api.universe, "refresh_single_ticker", _fake_refresh(db_session_factory)
    )

    # Lowercase input gets normalized; the first EQUITY result wins.
    resp = client.post("/api/universe/isin", json={"isin": "it0003132476"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ticker"] == "ENI.MI"
    assert body["monitored"] is False

    # The row is persisted: it now shows up in the normal universe listing.
    listing = client.get("/api/universe", params={"q": "ENI.MI"}).json()
    assert any(item["ticker"] == "ENI.MI" for item in listing["items"])


def test_isin_marks_monitored_when_symbol_exists(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: sessionmaker[Session],
) -> None:
    with db_session_factory() as db:
        db.add(Symbol(ticker="ENI.MI", name="Eni", currency="EUR", is_favorite=True))
        db.commit()

    _install_fake_search(monkeypatch, [{"ticker": "ENI.MI", "asset_type": "EQUITY"}])
    monkeypatch.setattr(
        universe_api.universe, "refresh_single_ticker", _fake_refresh(db_session_factory)
    )

    body = client.post("/api/universe/isin", json={"isin": "IT0003132476"}).json()
    assert body["monitored"] is True
    assert body["is_favorite"] is True
    assert body["symbol_id"] is not None
