"""Tests for ``get_pending_status`` and ``GET /api/evaluations/pending``.

Makes the learning loop visible before there is 7-day-old data: a fresh
symbol/recommendation history should report clearly how many recommendations
are pending vs. already scoreable, and when the next batch matures.

``ready_count`` promises only what the next evaluation can ACTUALLY score, so a
"ready" recommendation needs an entry price AND a stored close for its target
session — the helpers below seed both. Recommendations that are 7+ days old but
still lack that close are reported as ``awaiting_price_count`` instead.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.evaluation.evaluator import EVALUATION_WINDOW_DAYS, get_pending_status
from app.models import AnalysisRun, PriceHistory, Recommendation, Symbol


def _seed_recommendation(
    db: Session,
    *,
    symbol_id: int,
    created_at: datetime,
    evaluated: bool = False,
    entry_price: float | None = 100.0,
    with_target_close: bool = True,
) -> None:
    """Seed one unevaluated recommendation, by default fully scoreable.

    ``with_target_close`` also inserts the daily bar for the session 7 days
    after ``created_at`` (at exchange midnight, like the real refresh does), so
    the recommendation counts as ready. Pass ``False`` to simulate the
    calendar-mature-but-not-yet-priced case (weekend/holiday 7-day mark).
    """
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
            entry_price=entry_price,
            created_at=created_at,
        )
    )
    if with_target_close:
        target_session = (created_at + timedelta(days=EVALUATION_WINDOW_DAYS)).date()
        ts = datetime.combine(target_session, datetime.min.time())
        exists = (
            db.query(PriceHistory)
            .filter(
                PriceHistory.symbol_id == symbol_id,
                PriceHistory.interval == "1d",
                PriceHistory.ts == ts,
            )
            .one_or_none()
        )
        if exists is None:
            db.add(
                PriceHistory(
                    symbol_id=symbol_id, ts=ts, interval="1d",
                    open=105.0, high=105.0, low=105.0, close=105.0, volume=1_000_000.0,
                )
            )
    db.commit()


def test_get_pending_status_empty(db_session_factory: sessionmaker[Session]) -> None:
    db = db_session_factory()
    try:
        status = get_pending_status(db)
    finally:
        db.close()
    assert status == {
        "pending_count": 0,
        "ready_count": 0,
        "awaiting_price_count": 0,
        "next_evaluable_at": None,
    }


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
    assert status["awaiting_price_count"] == 0
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

    assert status == {
        "pending_count": 0,
        "ready_count": 1,
        "awaiting_price_count": 0,
        "next_evaluable_at": None,
    }


def test_calendar_mature_without_target_close_is_not_ready(
    db_session_factory: sessionmaker[Session],
) -> None:
    """The exact bug this fixes: 7+ days old but no close for the target session
    (e.g. the 7-day mark fell on a weekend) must NOT be announced as ready — the
    evaluator cannot score it yet and would leave it untouched."""
    db = db_session_factory()
    try:
        symbol = Symbol(ticker="WKND", name="Weekend Corp.")
        db.add(symbol)
        db.commit()
        db.refresh(symbol)

        _seed_recommendation(
            db,
            symbol_id=symbol.id,
            created_at=datetime.utcnow() - timedelta(days=9),
            with_target_close=False,
        )

        status = get_pending_status(db)
    finally:
        db.close()

    assert status["ready_count"] == 0
    assert status["awaiting_price_count"] == 1
    assert status["pending_count"] == 0


def test_calendar_mature_without_entry_price_is_not_ready(
    db_session_factory: sessionmaker[Session],
) -> None:
    """No entry price means no computable return: also not "ready"."""
    db = db_session_factory()
    try:
        symbol = Symbol(ticker="NOENT", name="No Entry Corp.")
        db.add(symbol)
        db.commit()
        db.refresh(symbol)

        _seed_recommendation(
            db,
            symbol_id=symbol.id,
            created_at=datetime.utcnow() - timedelta(days=9),
            entry_price=None,
        )

        status = get_pending_status(db)
    finally:
        db.close()

    assert status["ready_count"] == 0
    assert status["awaiting_price_count"] == 1


def test_pending_endpoint_returns_status(client: TestClient) -> None:
    resp = client.get("/api/evaluations/pending")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "pending_count": 0,
        "ready_count": 0,
        "awaiting_price_count": 0,
        "next_evaluable_at": None,
    }


def test_pending_endpoint_route_does_not_collide_with_evaluation_id(
    client: TestClient,
) -> None:
    # "pending" must never be swallowed by GET /evaluations/{evaluation_id}.
    resp = client.get("/api/evaluations/pending")
    assert resp.status_code == 200
