"""API tests for ``GET /api/symbols/{symbol_id}/runs/latest`` (offline).

Seeds a symbol with two runs (an older COMPLETED one and a newer RUNNING one
carrying two analyses rows) and asserts the endpoint returns the most recent run
serialized like ``GET /runs/{run_id}``, plus the two 404 branches (missing
symbol, symbol without any run).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.models import Analysis, AnalysisRun, Symbol


def _seed_symbol_with_runs(factory: sessionmaker[Session]) -> tuple[int, int]:
    """Insert a symbol + an older COMPLETED run and a newer RUNNING run.

    The RUNNING run gets two analyses rows (technical, fundamentals). Returns
    ``(symbol_id, running_run_id)``.
    """
    session = factory()
    try:
        symbol = Symbol(ticker="LAT1", name="Latest Inc.", exchange="NASDAQ", currency="USD")
        session.add(symbol)
        session.commit()
        session.refresh(symbol)
        symbol_id = symbol.id

        now = datetime.utcnow()
        older = AnalysisRun(
            symbol_id=symbol_id,
            status="COMPLETED",
            trigger="SCHEDULED",
            started_at=now - timedelta(hours=2),
            finished_at=now - timedelta(hours=1, minutes=55),
        )
        newer = AnalysisRun(
            symbol_id=symbol_id,
            status="RUNNING",
            trigger="MANUAL",
            started_at=now,
        )
        session.add_all([older, newer])
        session.commit()
        session.refresh(newer)
        running_run_id = newer.id

        session.add_all(
            [
                Analysis(
                    run_id=running_run_id,
                    symbol_id=symbol_id,
                    agent_name="technical",
                    status="OK",
                    summary_it="Trend positivo.",
                ),
                Analysis(
                    run_id=running_run_id,
                    symbol_id=symbol_id,
                    agent_name="fundamentals",
                    status="OK",
                    summary_it="Valutazione equa.",
                ),
            ]
        )
        session.commit()
        return symbol_id, running_run_id
    finally:
        session.close()


def test_latest_returns_most_recent_running_run(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    symbol_id, running_run_id = _seed_symbol_with_runs(db_session_factory)

    resp = client.get(f"/api/symbols/{symbol_id}/runs/latest")
    assert resp.status_code == 200
    body = resp.json()

    assert body["id"] == running_run_id
    assert body["status"] == "RUNNING"
    assert body["trigger"] == "MANUAL"
    assert body["ticker"] == "LAT1"
    assert body["symbol_id"] == symbol_id
    assert body["recommendation"] is None

    assert len(body["analyses"]) == 2
    assert {a["agent_name"] for a in body["analyses"]} == {"technical", "fundamentals"}


def test_latest_missing_symbol_returns_404(client: TestClient) -> None:
    resp = client.get("/api/symbols/999999/runs/latest")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Simbolo non trovato."


def test_latest_symbol_without_runs_returns_404(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    session = db_session_factory()
    try:
        symbol = Symbol(ticker="NORUN", name="No Runs Inc.", currency="USD")
        session.add(symbol)
        session.commit()
        session.refresh(symbol)
        symbol_id = symbol.id
    finally:
        session.close()

    resp = client.get(f"/api/symbols/{symbol_id}/runs/latest")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Nessuna analisi per questo simbolo."
