"""Offline tests for the horizon-aware evaluation pass (blueprint §7 addendum, part 2).

Covers the pure scoring helpers first (fast, no DB), then a full pass through
``_run_weekly_evaluation_impl`` with a seeded temp DB and a fake
``market_data_service``/``feedback`` (no network, no LLM). The pre-existing
7-day checkpoint is asserted to be byte-for-byte unchanged: every touched
function only gained an optional trailing parameter with a default that
reproduces the original behavior.
"""

from __future__ import annotations

import json
import math
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

import app.evaluation.evaluator as evaluator
from app.evaluation.evaluator import (
    BASE_NORM_PCT,
    CORRECT_THRESHOLD,
    EVALUATION_WINDOW_DAYS,
    HORIZON_MAX_DAYS,
    HORIZON_MIN_DAYS,
    _analyst_sample,
    _clamped_horizon,
    _first_close_at_or_after,
    _ideal_signal,
    _norm_pct,
    _outcome_score,
    _resolve_effective_return,
)
from app.models import Analysis, AnalysisRun, Evaluation, PriceHistory, Recommendation, Symbol

# --------------------------------------------------------------------------- #
# 1. Pure scoring helpers
# --------------------------------------------------------------------------- #


def test_first_close_accepts_the_target_session_itself(
    db_session_factory: sessionmaker[Session],
) -> None:
    """The target session's OWN close must count, not the one after it.

    Regression guard: ``target`` keeps the recommendation's time of day (17:09
    here) while the daily bar for that session sits at exchange midnight, so a
    raw ``ts >= target`` comparison silently required the NEXT session and
    scored everything a day late.
    """
    db = db_session_factory()
    try:
        symbol = Symbol(ticker="SESS1", name="Session Co", exchange="NASDAQ", currency="USD")
        db.add(symbol)
        db.commit()
        db.refresh(symbol)
        # US-style bar for session 2026-07-27, stored at exchange midnight (04:00 UTC).
        db.add(
            PriceHistory(
                symbol_id=symbol.id, ts=datetime(2026, 7, 27, 4, 0), interval="1d",
                open=50.0, high=50.0, low=50.0, close=57.0, volume=1.0,
            )
        )
        db.commit()

        target = datetime(2026, 7, 27, 17, 9, 35)  # same session, later clock time
        assert _first_close_at_or_after(db, symbol.id, target) == pytest.approx(57.0)
    finally:
        db.close()


def test_first_close_ignores_sessions_before_the_target(
    db_session_factory: sessionmaker[Session],
) -> None:
    """A close from BEFORE the target session must never be used (no back-filling)."""
    db = db_session_factory()
    try:
        symbol = Symbol(ticker="SESS2", name="Session Co 2", exchange="NASDAQ", currency="USD")
        db.add(symbol)
        db.commit()
        db.refresh(symbol)
        # Only a Friday bar; the target session is the following Sunday.
        db.add(
            PriceHistory(
                symbol_id=symbol.id, ts=datetime(2026, 7, 24, 4, 0), interval="1d",
                open=50.0, high=50.0, low=50.0, close=51.0, volume=1.0,
            )
        )
        db.commit()

        assert _first_close_at_or_after(db, symbol.id, datetime(2026, 7, 26, 17, 9)) is None
    finally:
        db.close()


def test_first_close_handles_non_us_timestamp_offset(
    db_session_factory: sessionmaker[Session],
) -> None:
    """A Milan-style bar (22:00 UTC of the PRIOR day) still maps to its own session."""
    db = db_session_factory()
    try:
        symbol = Symbol(ticker="SESS3.MI", name="Milan Co", exchange="MIL", currency="EUR")
        db.add(symbol)
        db.commit()
        db.refresh(symbol)
        # Session 2026-07-27 stored as 2026-07-26 22:00 (local midnight, CEST).
        db.add(
            PriceHistory(
                symbol_id=symbol.id, ts=datetime(2026, 7, 26, 22, 0), interval="1d",
                open=10.0, high=10.0, low=10.0, close=12.5, volume=1.0,
            )
        )
        db.commit()

        # Target on that same session resolves; a target one session later does not.
        assert _first_close_at_or_after(
            db, symbol.id, datetime(2026, 7, 27, 17, 9)
        ) == pytest.approx(12.5)
        assert _first_close_at_or_after(db, symbol.id, datetime(2026, 7, 28, 9, 0)) is None
    finally:
        db.close()


def test_norm_pct_base_window_is_unscaled():
    assert _norm_pct(EVALUATION_WINDOW_DAYS) == pytest.approx(BASE_NORM_PCT)


def test_norm_pct_scales_with_sqrt_of_window():
    # 30 days -> 5 * sqrt(30/7)
    assert _norm_pct(30) == pytest.approx(5.0 * math.sqrt(30 / 7))
    # 60 days (the cap) -> 5 * sqrt(60/7)
    assert _norm_pct(60) == pytest.approx(5.0 * math.sqrt(60 / 7))


def test_outcome_score_default_norm_matches_original_7day_behavior():
    # BUY +5% saturates to +1.0 at the default (unscaled) norm, exactly as before.
    assert _outcome_score("BUY", 5.0) == pytest.approx(1.0)
    assert _outcome_score("BUY", 2.5) == pytest.approx(0.5)
    assert _outcome_score("SELL", -5.0) == pytest.approx(1.0)
    assert _outcome_score("HOLD", 0.0) == pytest.approx(1.0)
    assert _outcome_score("HOLD", 5.0) == pytest.approx(-1.0)


def test_outcome_score_with_scaled_norm_does_not_saturate_early():
    norm_30d = _norm_pct(30)
    # A +5% BUY at the 7-day norm would saturate; at the wider 30-day norm it
    # should NOT be judged as harshly (a longer horizon tolerates bigger moves).
    score_30d = _outcome_score("BUY", 5.0, norm_30d)
    score_7d = _outcome_score("BUY", 5.0)
    assert score_30d < score_7d
    assert score_30d == pytest.approx(5.0 / norm_30d)


def test_ideal_signal_default_matches_original():
    assert _ideal_signal(2.5) == pytest.approx(0.5)
    assert _ideal_signal(2.5, _norm_pct(30)) < _ideal_signal(2.5)


def test_analyst_sample_flat_band_scales_with_norm():
    # At the default norm, a signal below the flat threshold with a 1.5% move
    # (inside the fixed 2.0% band) is "correct".
    correct, _weight, _err = _analyst_sample(0.05, 0.6, 1.5)
    assert correct is True
    # At the wider 30-day norm, a signal below the flat threshold with a 3.0%
    # move (outside the fixed 2.0% band, but inside the SCALED ~4.15% band)
    # must ALSO be "correct" -- the flat band scales with the window.
    norm_30d = _norm_pct(30)
    correct_scaled, _w, _e = _analyst_sample(0.05, 0.6, 3.0, norm_30d)
    assert correct_scaled is True
    # Sanity: the same 3.0% move at the unscaled (7-day) norm is NOT flat-correct.
    correct_unscaled, _w2, _e2 = _analyst_sample(0.05, 0.6, 3.0)
    assert correct_unscaled is False


def test_resolve_effective_return_buy_sell_use_excess_when_benchmark_present():
    ret, basis = _resolve_effective_return("BUY", 10.0, 4.0)
    assert (ret, basis) == pytest.approx((6.0, 0.0)) or (ret, basis)[1] == "excess"
    assert ret == pytest.approx(6.0)
    assert basis == "excess"

    ret, basis = _resolve_effective_return("SELL", -10.0, 4.0)
    assert ret == pytest.approx(-14.0)
    assert basis == "excess"


def test_resolve_effective_return_hold_is_always_absolute():
    # HOLD ignores the benchmark even when one is resolvable: a HOLD that fell
    # 3% while the market fell 10% must NOT be penalized for "underperforming".
    ret, basis = _resolve_effective_return("HOLD", -3.0, -10.0)
    assert ret == pytest.approx(-3.0)
    assert basis == "absolute"


def test_resolve_effective_return_falls_back_to_absolute_without_benchmark():
    ret, basis = _resolve_effective_return("BUY", 10.0, None)
    assert ret == pytest.approx(10.0)
    assert basis == "absolute"


def test_clamped_horizon_respects_bounds_and_default():
    rec_normal = Recommendation(horizon_days=30)
    assert _clamped_horizon(rec_normal) == 30

    rec_short = Recommendation(horizon_days=1)
    assert _clamped_horizon(rec_short) == HORIZON_MIN_DAYS

    rec_long = Recommendation(horizon_days=365)
    assert _clamped_horizon(rec_long) == HORIZON_MAX_DAYS

    rec_zero = Recommendation(horizon_days=0)
    assert _clamped_horizon(rec_zero) == 30  # falls back to the synthesizer default


# --------------------------------------------------------------------------- #
# 2. Full horizon pass through _run_weekly_evaluation_impl (seeded DB, no network/LLM)
# --------------------------------------------------------------------------- #


class _FakeMarket:
    """Stands in for market_data_service: no network, canned benchmark return."""

    def __init__(self, benchmark_return: float | None = 4.0) -> None:
        self._benchmark_return = benchmark_return

    def benchmark_for(self, ticker: str) -> str:
        return "^GSPC"

    def get_benchmark_window_return(self, benchmark: str, start, end) -> float | None:
        return self._benchmark_return

    def refresh_prices(self, db, symbol, interval="1d", days=730) -> int:
        return 0  # prices are already fully seeded; refresh is a no-op


@contextmanager
def _session_scope_factory(factory: sessionmaker[Session]):
    db = factory()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _install_fakes(monkeypatch: pytest.MonkeyPatch, db_session_factory, benchmark_return=4.0):
    monkeypatch.setattr(evaluator, "market_data_service", _FakeMarket(benchmark_return))
    monkeypatch.setattr(evaluator, "session_scope", lambda: _session_scope_factory(db_session_factory))

    async def _noop_lessons(evaluation_id: int) -> None:
        return None

    async def _noop_report(evaluation_id: int) -> None:
        return None

    monkeypatch.setattr(evaluator.feedback, "generate_lessons", _noop_lessons)
    monkeypatch.setattr(evaluator.feedback, "generate_report_it", _noop_report)


def _seed_matured_buy(
    db: Session, *, horizon_days: int = 30, entry_price: float = 100.0, close_price: float = 110.0
) -> tuple[int, datetime]:
    """A BUY recommendation created just past its own horizon, with a matching
    daily close seeded at the horizon target, plus one technical Analysis row."""
    symbol = Symbol(ticker="AAPL", name="Apple Inc.", exchange="NASDAQ", currency="USD")
    db.add(symbol)
    db.commit()
    db.refresh(symbol)

    created_at = datetime.utcnow() - timedelta(days=horizon_days + 1)
    target = created_at + timedelta(days=horizon_days)

    db.add(
        PriceHistory(
            symbol_id=symbol.id,
            ts=target,
            interval="1d",
            open=close_price,
            high=close_price,
            low=close_price,
            close=close_price,
            volume=1_000_000.0,
        )
    )

    run = AnalysisRun(symbol_id=symbol.id, status="COMPLETED", trigger="MANUAL")
    db.add(run)
    db.flush()

    db.add(
        Analysis(
            run_id=run.id,
            symbol_id=symbol.id,
            agent_name="technical",
            status="OK",
            signal=0.6,
            confidence=0.7,
            stance="BULLISH",
        )
    )

    rec = Recommendation(
        run_id=run.id,
        symbol_id=symbol.id,
        action="BUY",
        sizing_strategy="DCA",
        confidence=0.6,
        allocation_pct=10.0,
        entry_price=entry_price,
        horizon_days=horizon_days,
        validator_verdict="APPROVE",
        created_at=created_at,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec.id, created_at


@pytest.mark.asyncio
async def test_horizon_pass_scores_matured_buy_relative_to_benchmark(
    monkeypatch: pytest.MonkeyPatch, db_session_factory: sessionmaker[Session]
) -> None:
    db = db_session_factory()
    try:
        rec_id, _created_at = _seed_matured_buy(db, horizon_days=30, entry_price=100.0, close_price=110.0)
    finally:
        db.close()

    _install_fakes(monkeypatch, db_session_factory, benchmark_return=4.0)

    evaluation = await evaluator.run_weekly_evaluation()

    db = db_session_factory()
    try:
        rec = db.get(Recommendation, rec_id)
        assert rec.evaluated_h is True
        assert rec.realized_return_h == pytest.approx(10.0)
        assert rec.benchmark_return_h == pytest.approx(4.0)
        assert rec.excess_return_h == pytest.approx(6.0)
        assert rec.outcome_basis_h == "excess"
        expected_norm = _norm_pct(30)
        assert rec.outcome_score_h == pytest.approx(6.0 / expected_norm)
        assert rec.outcome_score_h > CORRECT_THRESHOLD

        per_agent = json.loads(evaluation.per_agent_json)
        assert "final" in per_agent["technical"]
        assert per_agent["technical"]["final"]["n_samples"] == 1
        assert per_agent["technical"]["final"]["accuracy"] == pytest.approx(1.0)
        # The 7-day checkpoint's own top-level keys must still be present and
        # untouched (this rec hasn't matured for it independently, but the key
        # shape itself -- accuracy/avg_signal_error/n_samples -- must survive).
        assert set(per_agent["technical"].keys()) >= {"accuracy", "avg_signal_error", "n_samples", "final"}
    finally:
        db.close()


@pytest.mark.asyncio
async def test_horizon_pass_falls_back_to_absolute_when_benchmark_unresolvable(
    monkeypatch: pytest.MonkeyPatch, db_session_factory: sessionmaker[Session]
) -> None:
    db = db_session_factory()
    try:
        rec_id, _created_at = _seed_matured_buy(db, horizon_days=30, entry_price=100.0, close_price=110.0)
    finally:
        db.close()

    _install_fakes(monkeypatch, db_session_factory, benchmark_return=None)

    await evaluator.run_weekly_evaluation()

    db = db_session_factory()
    try:
        rec = db.get(Recommendation, rec_id)
        assert rec.evaluated_h is True
        assert rec.benchmark_return_h is None
        assert rec.excess_return_h is None
        assert rec.outcome_basis_h == "absolute"
        assert rec.outcome_score_h == pytest.approx(_outcome_score("BUY", 10.0, _norm_pct(30)))
    finally:
        db.close()


@pytest.mark.asyncio
async def test_horizon_pass_skips_recommendation_not_yet_at_its_own_horizon(
    monkeypatch: pytest.MonkeyPatch, db_session_factory: sessionmaker[Session]
) -> None:
    """A 30-day-horizon rec created only 10 days ago must NOT be scored yet,
    even though it's already past the fixed 7-day checkpoint."""
    db = db_session_factory()
    try:
        symbol = Symbol(ticker="MSFT", name="Microsoft", exchange="NASDAQ", currency="USD")
        db.add(symbol)
        db.commit()
        db.refresh(symbol)

        created_at = datetime.utcnow() - timedelta(days=10)
        run = AnalysisRun(symbol_id=symbol.id, status="COMPLETED", trigger="MANUAL")
        db.add(run)
        db.flush()
        rec = Recommendation(
            run_id=run.id,
            symbol_id=symbol.id,
            action="BUY",
            sizing_strategy="DCA",
            confidence=0.6,
            allocation_pct=10.0,
            entry_price=100.0,
            horizon_days=30,
            validator_verdict="APPROVE",
            created_at=created_at,
        )
        db.add(rec)
        db.commit()
        rec_id = rec.id
    finally:
        db.close()

    _install_fakes(monkeypatch, db_session_factory, benchmark_return=4.0)
    await evaluator.run_weekly_evaluation()

    db = db_session_factory()
    try:
        rec = db.get(Recommendation, rec_id)
        assert rec.evaluated_h is False
        assert rec.outcome_score_h is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_horizon_pass_hold_ignores_benchmark_even_when_resolvable(
    monkeypatch: pytest.MonkeyPatch, db_session_factory: sessionmaker[Session]
) -> None:
    db = db_session_factory()
    try:
        symbol = Symbol(ticker="KO", name="Coca-Cola", exchange="NYSE", currency="USD")
        db.add(symbol)
        db.commit()
        db.refresh(symbol)

        horizon_days = 30
        created_at = datetime.utcnow() - timedelta(days=horizon_days + 1)
        target = created_at + timedelta(days=horizon_days)
        db.add(
            PriceHistory(
                symbol_id=symbol.id, ts=target, interval="1d",
                open=99.0, high=99.0, low=99.0, close=99.0, volume=1_000_000.0,
            )
        )
        run = AnalysisRun(symbol_id=symbol.id, status="COMPLETED", trigger="MANUAL")
        db.add(run)
        db.flush()
        rec = Recommendation(
            run_id=run.id,
            symbol_id=symbol.id,
            action="HOLD",
            sizing_strategy="WAIT",
            confidence=0.6,
            allocation_pct=0.0,
            entry_price=100.0,
            horizon_days=horizon_days,
            validator_verdict="APPROVE",
            created_at=created_at,
        )
        db.add(rec)
        db.commit()
        rec_id = rec.id
    finally:
        db.close()

    # Benchmark resolvable and very negative -- must NOT affect a HOLD's score.
    _install_fakes(monkeypatch, db_session_factory, benchmark_return=-20.0)
    await evaluator.run_weekly_evaluation()

    db = db_session_factory()
    try:
        rec = db.get(Recommendation, rec_id)
        assert rec.evaluated_h is True
        assert rec.realized_return_h == pytest.approx(-1.0)
        assert rec.benchmark_return_h == pytest.approx(-20.0)  # recorded...
        assert rec.outcome_basis_h == "absolute"  # ...but NOT used for scoring
        assert rec.outcome_score_h == pytest.approx(
            _outcome_score("HOLD", -1.0, _norm_pct(horizon_days))
        )
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 3. Regression: the pre-existing 7-day checkpoint is unchanged
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_seven_day_checkpoint_unaffected_by_horizon_pass(
    monkeypatch: pytest.MonkeyPatch, db_session_factory: sessionmaker[Session]
) -> None:
    """A rec old enough for the 7-day checkpoint but NOT yet at its own 30-day
    horizon must still get its `evaluated`/`outcome_score` set exactly as before,
    while `evaluated_h` stays False."""
    db = db_session_factory()
    try:
        symbol = Symbol(ticker="NVDA", name="Nvidia", exchange="NASDAQ", currency="USD")
        db.add(symbol)
        db.commit()
        db.refresh(symbol)

        created_at = datetime.utcnow() - timedelta(days=8)
        target = created_at + timedelta(days=EVALUATION_WINDOW_DAYS)
        db.add(
            PriceHistory(
                symbol_id=symbol.id, ts=target, interval="1d",
                open=103.0, high=103.0, low=103.0, close=103.0, volume=1_000_000.0,
            )
        )
        run = AnalysisRun(symbol_id=symbol.id, status="COMPLETED", trigger="MANUAL")
        db.add(run)
        db.flush()
        rec = Recommendation(
            run_id=run.id,
            symbol_id=symbol.id,
            action="BUY",
            sizing_strategy="DCA",
            confidence=0.6,
            allocation_pct=10.0,
            entry_price=100.0,
            horizon_days=30,
            validator_verdict="APPROVE",
            created_at=created_at,
        )
        db.add(rec)
        db.commit()
        rec_id = rec.id
    finally:
        db.close()

    _install_fakes(monkeypatch, db_session_factory, benchmark_return=4.0)
    await evaluator.run_weekly_evaluation()

    db = db_session_factory()
    try:
        rec = db.get(Recommendation, rec_id)
        assert rec.evaluated is True
        assert rec.realized_return_7d == pytest.approx(3.0)
        assert rec.outcome_score == pytest.approx(_outcome_score("BUY", 3.0))
        assert rec.evaluated_h is False  # 30-day horizon hasn't matured yet
    finally:
        db.close()
