"""Test dell'endpoint del portafoglio simulato (blueprint §6 addendum)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.engine.sim_book import SIM_BASE_NOTIONAL, SIM_FEE_PCT
from app.models import AnalysisRun, PriceHistory, Recommendation, SimPosition, Symbol


def _symbol(db: Session, ticker: str, currency: str = "USD") -> Symbol:
    symbol = Symbol(ticker=ticker, name=f"{ticker} Inc.", exchange="NASDAQ", currency=currency)
    db.add(symbol)
    db.commit()
    db.refresh(symbol)
    return symbol


def _position(
    db: Session,
    symbol: Symbol,
    *,
    status: str = "OPEN",
    weight_pct: float = 10.0,
    cost_total: float = 1000.0,
    shares_open: float = 10.0,
    realized_pnl: float | None = None,
    close_reason: str | None = None,
    opened: date | None = None,
) -> SimPosition:
    """Semina una posizione, con il consiglio BUY che l'ha aperta.

    Il consiglio è obbligatorio: ``open_recommendation_id`` è la chiave di
    idempotenza del motore, e il fixture di test attiva ``PRAGMA
    foreign_keys=ON``, quindi un id inventato viene giustamente rifiutato.
    """
    run = AnalysisRun(symbol_id=symbol.id, status="COMPLETED", trigger="MANUAL")
    db.add(run)
    db.flush()
    reco = Recommendation(
        run_id=run.id,
        symbol_id=symbol.id,
        action="BUY",
        sizing_strategy="ALL_IN",
        confidence=0.6,
        allocation_pct=weight_pct,
        validator_verdict="APPROVE",
    )
    db.add(reco)
    db.commit()
    db.refresh(reco)

    pos = SimPosition(
        symbol_id=symbol.id,
        open_recommendation_id=reco.id,
        currency=symbol.currency,
        weight_pct=weight_pct,
        planned_notional=SIM_BASE_NOTIONAL * weight_pct / 100.0,
        tranches_total=1,
        tranches_filled=1,
        shares_open=shares_open if status == "OPEN" else 0.0,
        cost_total=cost_total,
        avg_entry_price=cost_total / shares_open if shares_open else None,
        horizon_days=30,
        opened_session=opened or (datetime.utcnow().date() - timedelta(days=5)),
        status=status,
        close_reason=close_reason,
        realized_pnl=realized_pnl,
        realized_pnl_pct=(realized_pnl / cost_total * 100.0) if realized_pnl is not None else None,
        closed_session=(datetime.utcnow().date() - timedelta(days=1)) if status == "CLOSED" else None,
    )
    db.add(pos)
    db.commit()
    db.refresh(pos)
    return pos


def _bar(db: Session, symbol_id: int, close: float, day_offset: int = 0) -> None:
    session = datetime.utcnow().date() - timedelta(days=day_offset)
    db.add(
        PriceHistory(
            symbol_id=symbol_id,
            ts=datetime.combine(session, datetime.min.time()),
            interval="1d",
            open=close, high=close, low=close, close=close, volume=1.0,
        )
    )
    db.commit()


def test_empty_book_returns_a_clean_shape(client: TestClient) -> None:
    resp = client.get("/api/sim/portfolio")
    assert resp.status_code == 200
    body = resp.json()
    assert body["n_open"] == 0
    assert body["n_closed"] == 0
    assert body["open_positions"] == []
    assert body["by_currency"] == {}
    assert body["stats"]["status"] == "dati_insufficienti"
    assert body["base_notional"] == SIM_BASE_NOTIONAL
    # La commissione simulata è zero per scelta dichiarata.
    assert body["fee_pct"] == SIM_FEE_PCT == 0.0


def test_open_position_is_marked_to_market(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    db = db_session_factory()
    try:
        sym = _symbol(db, "AAA")
        _position(db, sym, cost_total=1000.0, shares_open=10.0)
        _bar(db, sym.id, 120.0)  # 10 azioni x 120 = 1200
    finally:
        db.close()

    body = client.get("/api/sim/portfolio").json()
    assert body["n_open"] == 1
    pos = body["open_positions"][0]
    assert pos["ticker"] == "AAA"
    assert pos["market_value"] == pytest.approx(1200.0)
    assert pos["unrealized_pnl"] == pytest.approx(200.0)
    assert pos["unrealized_pnl_pct"] == pytest.approx(20.0)
    assert body["gross_exposure_pct"] == pytest.approx(10.0)


def test_open_position_without_a_price_declares_null_never_estimates(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    db = db_session_factory()
    try:
        sym = _symbol(db, "NOPRICE")
        _position(db, sym)
    finally:
        db.close()

    body = client.get("/api/sim/portfolio").json()
    pos = body["open_positions"][0]
    assert pos["market_value"] is None
    assert pos["unrealized_pnl"] is None
    # E l'aggregato si dichiara parziale invece di sembrare completo.
    assert body["by_currency"]["USD"]["unrealized_pnl"] is None


def test_pnl_is_reported_per_currency_and_never_summed(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    db = db_session_factory()
    try:
        usd = _symbol(db, "USDX", "USD")
        eur = _symbol(db, "EURX", "EUR")
        _position(db, usd, status="CLOSED", realized_pnl=150.0, close_reason="TAKE_PROFIT")
        _position(db, eur, status="CLOSED", realized_pnl=-40.0, close_reason="STOP_LOSS")
    finally:
        db.close()

    body = client.get("/api/sim/portfolio").json()
    assert body["n_closed"] == 2
    assert body["by_currency"]["USD"]["realized_pnl"] == pytest.approx(150.0)
    assert body["by_currency"]["EUR"]["realized_pnl"] == pytest.approx(-40.0)
    # Nessun totale globale: non esiste un tasso di cambio in questa app.
    assert "total_pnl" not in body


def test_stale_positions_are_exposed_separately_not_hidden(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    """Una posizione scaduta senza prezzi si dichiara, non si chiude a un prezzo inventato."""
    db = db_session_factory()
    try:
        sym = _symbol(db, "DELISTED")
        _position(db, sym, status="STALE")
    finally:
        db.close()

    body = client.get("/api/sim/portfolio").json()
    assert body["n_stale"] == 1
    assert body["n_open"] == 0
    assert [p["ticker"] for p in body["stale_positions"]] == ["DELISTED"]
    assert body["open_positions"] == []


def test_stats_are_withheld_below_the_floor_but_counts_are_visible(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    db = db_session_factory()
    try:
        for i in range(3):
            sym = _symbol(db, f"S{i}")
            _position(
                db, sym, status="CLOSED", realized_pnl=10.0, close_reason="HORIZON",
                opened=date(2026, 1, 5) + timedelta(days=7 * i),
            )
    finally:
        db.close()

    stats = client.get("/api/sim/portfolio").json()["stats"]
    assert stats["status"] == "dati_insufficienti"
    assert stats["n"] == 3
    assert stats["stop_hit_rate"] is None
    # I conteggi restano, così l'interfaccia può dire quanto manca.
    assert stats["min_n"] == 12
    assert stats["min_symbols"] == 4


def test_cumulative_pnl_is_not_truncated_by_the_recent_closed_page_size(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    """Il P&L cumulato deve contare TUTTE le chiusure, non solo quelle mostrate."""
    db = db_session_factory()
    try:
        for i in range(5):
            sym = _symbol(db, f"C{i}")
            _position(db, sym, status="CLOSED", realized_pnl=100.0, close_reason="HORIZON")
    finally:
        db.close()

    body = client.get("/api/sim/portfolio?recent_closed=2").json()
    assert len(body["recent_closed"]) == 2  # la pagina è troncata...
    assert body["n_closed"] == 5  # ...ma il conteggio no
    assert body["by_currency"]["USD"]["realized_pnl"] == pytest.approx(500.0)


def test_there_is_no_endpoint_to_write_the_book_by_hand(client: TestClient) -> None:
    """Il libro è scrivibile solo dal motore deterministico: è ciò che lo rende
    riproducibile, e un POST manuale distruggerebbe quella proprietà."""
    assert client.post("/api/sim/portfolio", json={}).status_code in (404, 405)
    assert client.delete("/api/sim/portfolio").status_code in (404, 405)
