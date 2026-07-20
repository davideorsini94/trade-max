"""Tests for ``get_pending_status`` and ``GET /api/evaluations/pending``.

Makes the learning loop visible before there is 7-day-old data: a fresh
symbol/recommendation history should report clearly how many recommendations
are pending vs. already scoreable, and when the next batch matures.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.evaluation.evaluator import EVALUATION_WINDOW_DAYS, get_pending_status
from app.models import AnalysisRun, Recommendation, Symbol


def _seed_recommendation(
    db: Session, *, symbol_id: int, created_at: datetime, evaluated: bool = False
) -> None:
    run = AnalysisRun(symbol_id=symbol_id, status="COMPLETED", trigger="MANUAL")
    db.add(run)
    db.flush()
    db.add(
        Recommendation(
            run_id=run.id,
            symbol_id=symbol_id,
            action="HOLD",
            sizing_strategy="WAIT",
            confidence=0.5,
            validator_verdict="APPROVE",
            evaluated=evaluated,
            created_at=created_at,
        )
    )
    db.commit()


def test_get_pending_status_empty(db_session_factory: sessionmaker[Session]) -> None:
    db = db_session_factory()
    try:
        status = get_pending_status(db)
    finally:
        db.close()
    assert status == {"pending_count": 0, "ready_count": 0, "next_evaluable_at": None}


def test_get_pending_status_mixed(db_session_factory: sessionmaker[Session]) -> None:
    db = db_session_factory()
    try:
        symbol = Symbol(ticker="AAPL", name="Apple Inc.")
        db.add(symbol)
        db.commit()
        db.refresh(symbol)

        now = datetime.utcnow()
        # Already past the 7-day window: ready to be scored on the next run.
        _seed_recommendation(db, symbol_id=symbol.id, created_at=now - timedelta(days=10))
        _seed_recommendation(db, symbol_id=symbol.id, created_at=now - timedelta(days=8))
        # Still maturing.
        oldest_pending = now - timedelta(days=2)
        _seed_recommendation(db, symbol_id=symbol.id, created_at=oldest_pending)
        _seed_recommendation(db, symbol_id=symbol.id, created_at=now - timedelta(hours=1))
        # Already evaluated: must be excluded entirely.
        _seed_recommendation(
            db, symbol_id=symbol.id, created_at=now - timedelta(days=20), evaluated=True
        )

        status = get_pending_status(db)
    finally:
        db.close()

    assert status["ready_count"] == 2
    assert status["pending_count"] == 2
    expected_next = oldest_pending + timedelta(days=EVALUATION_WINDOW_DAYS)
    assert status["next_evaluable_at"] is not None
    assert abs((status["next_evaluable_at"] - expected_next).total_seconds()) < 1


def test_get_pending_status_all_ready_has_no_next(
    db_session_factory: sessionmaker[Session],
) -> None:
    db = db_session_factory()
    try:
        symbol = Symbol(ticker="MSFT", name="Microsoft Corp.")
        db.add(symbol)
        db.commit()
        db.refresh(symbol)

        now = datetime.utcnow()
        _seed_recommendation(db, symbol_id=symbol.id, created_at=now - timedelta(days=9))

        status = get_pending_status(db)
    finally:
        db.close()

    assert status == {"pending_count": 0, "ready_count": 1, "next_evaluable_at": None}


def test_pending_endpoint_returns_status(client: TestClient) -> None:
    resp = client.get("/api/evaluations/pending")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"pending_count": 0, "ready_count": 0, "next_evaluable_at": None}


def test_pending_endpoint_route_does_not_collide_with_evaluation_id(
    client: TestClient,
) -> None:
    # "pending" must never be swallowed by GET /evaluations/{evaluation_id}.
    resp = client.get("/api/evaluations/pending")
    assert resp.status_code == 200
