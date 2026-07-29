"""Tests for the rolling performance summary (blueprint §7.1 addendum).

The bug being pinned shut: the Performance page showed the accuracy of ONE
evaluation batch. Batches held 1-12 samples, so the number swung 0.92 -> 0.33 ->
0.00 -> 1.00 -> 0.17 with no pipeline change at all — with n=1 the only possible
values ARE 0% and 100%. These tests cover the four defences: rolling window,
deduplication, honesty gate, and a Wilson interval that makes a thin sample
visibly thin.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.evaluation.features import FEATURE_STATS_MIN_N, FEATURE_STATS_MIN_SYMBOLS
from app.evaluation.summary import (
    ACCURACY_METRIC_VERSION,
    ROLLING_WINDOW_DAYS,
    compute_performance_summary,
    wilson_interval,
)
from app.models import AnalysisRun, Recommendation, Symbol


# --------------------------------------------------------------------------- #
# Wilson interval — the two pathological cases seen in production
# --------------------------------------------------------------------------- #


def test_wilson_makes_a_single_sample_visibly_uninformative():
    """k=0, n=1 must NOT read as a confident 0%."""
    low, high = wilson_interval(0, 1)
    assert low == pytest.approx(0.0, abs=1e-6)
    assert high == pytest.approx(0.7935, abs=0.001)


def test_wilson_on_the_real_inflated_batch():
    low, high = wilson_interval(11, 12)  # the 0.917 batch
    assert low == pytest.approx(0.646, abs=0.002)
    assert high == pytest.approx(0.985, abs=0.002)


def test_wilson_full_range_when_no_samples():
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_wilson_is_clamped_to_the_unit_interval():
    for k, n in ((0, 3), (3, 3), (1, 1)):
        low, high = wilson_interval(k, n)
        assert 0.0 <= low <= high <= 1.0


# --------------------------------------------------------------------------- #
# Seeding helpers
# --------------------------------------------------------------------------- #


def _seed(
    db: Session,
    ticker: str,
    *,
    created_at: datetime,
    action: str = "HOLD",
    outcome_score: float = 0.8,
    realized_return: float = 0.5,
    symbol_id: int | None = None,
) -> Recommendation:
    if symbol_id is None:
        symbol = Symbol(ticker=ticker, name=f"{ticker} Inc.", exchange="NASDAQ", currency="USD")
        db.add(symbol)
        db.commit()
        db.refresh(symbol)
        symbol_id = symbol.id

    run = AnalysisRun(symbol_id=symbol_id, status="COMPLETED", trigger="MANUAL")
    db.add(run)
    db.flush()
    rec = Recommendation(
        run_id=run.id,
        symbol_id=symbol_id,
        action=action,
        sizing_strategy="WAIT" if action == "HOLD" else "DCA",
        confidence=0.6,
        validator_verdict="APPROVE",
        entry_price=100.0,
        evaluated=True,
        outcome_score=outcome_score,
        realized_return_7d=realized_return,
        created_at=created_at,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


def _seed_cohort(db: Session, n_symbols: int, per_symbol: int, *, outcome_score: float = 0.8) -> None:
    """``n_symbols`` symbols x ``per_symbol`` recommendations, each in a DIFFERENT
    ISO week so nothing is deduplicated away."""
    now = datetime.utcnow()
    for s in range(n_symbols):
        symbol = Symbol(ticker=f"SYM{s}", name=f"S{s}", exchange="NASDAQ", currency="USD")
        db.add(symbol)
        db.commit()
        db.refresh(symbol)
        for i in range(per_symbol):
            _seed(
                db,
                f"SYM{s}",
                created_at=now - timedelta(days=i * 7 + 1),
                outcome_score=outcome_score,
                symbol_id=symbol.id,
            )


# --------------------------------------------------------------------------- #
# Deduplication
# --------------------------------------------------------------------------- #


def test_same_symbol_same_week_counts_once(db_session_factory: sessionmaker[Session]) -> None:
    """The direct cause of the inflated sample size: favourites re-analysed
    several times a day produced correlated rows counted as independent."""
    db = db_session_factory()
    try:
        symbol = Symbol(ticker="DUP", name="Dup", exchange="NASDAQ", currency="USD")
        db.add(symbol)
        db.commit()
        db.refresh(symbol)
        # Ancorato a un mercoledì FISSO, non a ``utcnow``: seminando in modo
        # relativo, quando il test girava di domenica sera il "+4h" scivolava
        # nella settimana ISO successiva e la deduplicazione ne trovava
        # legittimamente due. Il bug era nel test, non nella deduplicazione.
        now = datetime(2026, 7, 29, 12, 0)  # mercoledì
        base = now - timedelta(days=1)  # martedì, stessa settimana ISO
        for hours in (0, 4, 8):
            _seed(db, "DUP", created_at=base + timedelta(hours=hours), symbol_id=symbol.id)

        summary = compute_performance_summary(db, now=now)
        assert summary["n_raw"] == 3
        assert summary["n"] == 1
        assert summary["n_symbols"] == 1
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Honesty gate
# --------------------------------------------------------------------------- #


def test_enough_samples_but_too_few_symbols_is_insufficient(
    db_session_factory: sessionmaker[Session],
) -> None:
    db = db_session_factory()
    try:
        # 12 samples spread over only 3 symbols: n clears, symbols do not.
        _seed_cohort(db, n_symbols=3, per_symbol=4)
        summary = compute_performance_summary(db)
        assert summary["n"] == 12
        assert summary["n_symbols"] == 3
        assert summary["status"] == "dati_insufficienti"
        assert summary["accuracy"] is None
        # The counts are still exposed so the UI can explain the gap.
        assert summary["min_n"] == FEATURE_STATS_MIN_N
        assert summary["min_symbols"] == FEATURE_STATS_MIN_SYMBOLS
    finally:
        db.close()


def test_clearing_both_floors_yields_an_accuracy(
    db_session_factory: sessionmaker[Session],
) -> None:
    db = db_session_factory()
    try:
        _seed_cohort(db, n_symbols=4, per_symbol=3)  # 12 samples, 4 symbols
        summary = compute_performance_summary(db)
        assert summary["status"] == "ok"
        assert summary["n"] == 12
        assert summary["accuracy"] == pytest.approx(1.0)  # every seed scores 0.8 > 0.6
        assert summary["ci_low"] is not None and summary["ci_high"] is not None
    finally:
        db.close()


def test_continuous_score_is_reported_even_below_the_gate(
    db_session_factory: sessionmaker[Session],
) -> None:
    """The threshold-free metric degrades honestly, so it stays visible when the
    binary percentage is withheld."""
    db = db_session_factory()
    try:
        _seed(db, "ONE", created_at=datetime.utcnow() - timedelta(days=2), outcome_score=-0.4)
        summary = compute_performance_summary(db)
        assert summary["status"] == "dati_insufficienti"
        assert summary["accuracy"] is None
        assert summary["avg_outcome_score"] == pytest.approx(-0.4)
        assert summary["ci_low"] is not None  # interval present from the first sample
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Rolling window
# --------------------------------------------------------------------------- #


def test_window_excludes_older_recommendations(
    db_session_factory: sessionmaker[Session],
) -> None:
    db = db_session_factory()
    try:
        now = datetime.utcnow()
        _seed(db, "INSIDE", created_at=now - timedelta(days=ROLLING_WINDOW_DAYS - 1))
        _seed(db, "OUTSIDE", created_at=now - timedelta(days=ROLLING_WINDOW_DAYS + 1))
        summary = compute_performance_summary(db)
        assert summary["n"] == 1
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Definition consistency: one rule applied to the whole history
# --------------------------------------------------------------------------- #


def test_correctness_uses_the_current_hold_rule_across_all_history(
    db_session_factory: sessionmaker[Session],
) -> None:
    """A HOLD at 0.5 was "correct" under the old bar (0.2) and is not under the
    current one (0.6). Recomputing at read time is what removes the historical
    definition break without rewriting any stored row."""
    db = db_session_factory()
    try:
        _seed_cohort(db, n_symbols=4, per_symbol=3, outcome_score=0.5)
        summary = compute_performance_summary(db)
        assert summary["status"] == "ok"
        assert summary["k_correct"] == 0
        assert summary["accuracy"] == pytest.approx(0.0)
        assert summary["metric_version"] == ACCURACY_METRIC_VERSION
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Composition warning
# --------------------------------------------------------------------------- #


def test_hold_only_flag_and_action_mix(db_session_factory: sessionmaker[Session]) -> None:
    db = db_session_factory()
    try:
        now = datetime.utcnow()
        _seed(db, "H1", created_at=now - timedelta(days=2), action="HOLD")
        summary = compute_performance_summary(db)
        assert summary["hold_only"] is True
        assert summary["action_mix"] == {"HOLD": 1}

        _seed(db, "B1", created_at=now - timedelta(days=3), action="BUY", outcome_score=0.9)
        summary = compute_performance_summary(db)
        assert summary["hold_only"] is False
        assert summary["action_mix"] == {"HOLD": 1, "BUY": 1}
    finally:
        db.close()


def test_empty_cohort_degrades_cleanly(db_session_factory: sessionmaker[Session]) -> None:
    db = db_session_factory()
    try:
        summary = compute_performance_summary(db)
        assert summary["status"] == "dati_insufficienti"
        assert summary["n"] == 0
        assert summary["accuracy"] is None
        assert summary["avg_outcome_score"] is None
        assert summary["hold_only"] is False
        assert summary["per_agent"] == {}
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Endpoint
# --------------------------------------------------------------------------- #


def test_summary_endpoint_responds_and_does_not_collide_with_evaluation_id(
    client: TestClient,
) -> None:
    """"summary" must not be swallowed by GET /evaluations/{evaluation_id}."""
    resp = client.get("/api/evaluations/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "dati_insufficienti"
    assert body["window_days"] == ROLLING_WINDOW_DAYS
    assert body["metric_version"] == ACCURACY_METRIC_VERSION


# --------------------------------------------------------------------------- #
# Lessons gate: a micro-batch must not rewrite an agent's standing instructions
# --------------------------------------------------------------------------- #


async def test_tiny_batch_does_not_regenerate_lessons(
    monkeypatch: pytest.MonkeyPatch, db_session_factory: sessionmaker[Session]
) -> None:
    """Lessons are injected into every future prompt AND deactivate the previous
    ones, so a 1-sample batch would turn noise into a standing instruction."""
    import app.evaluation.evaluator as evaluator
    from contextlib import contextmanager

    @contextmanager
    def _scope():
        db = db_session_factory()
        try:
            yield db
            db.commit()
        finally:
            db.close()

    db = db_session_factory()
    try:
        _seed(db, "TINY", created_at=datetime.utcnow() - timedelta(days=8))
    finally:
        db.close()

    monkeypatch.setattr(evaluator, "session_scope", _scope)

    class _FakeMarket:
        def benchmark_for(self, ticker): return "^GSPC"
        def get_benchmark_window_return(self, b, s, e): return None
        def refresh_prices(self, db, symbol, interval="1d", days=730): return 0

    monkeypatch.setattr(evaluator, "market_data_service", _FakeMarket())

    calls: list[str] = []

    async def _lessons(evaluation_id: int) -> None:
        calls.append("lessons")

    async def _report(evaluation_id: int) -> None:
        calls.append("report")

    monkeypatch.setattr(evaluator.feedback, "generate_lessons", _lessons)
    monkeypatch.setattr(evaluator.feedback, "generate_report_it", _report)

    await evaluator.run_weekly_evaluation()

    # The seeded rec is already evaluated=True, so the 7-day queue is empty:
    # far below the floors either way. The report still runs.
    assert "lessons" not in calls
    assert "report" in calls
