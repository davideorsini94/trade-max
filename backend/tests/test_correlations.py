"""Offline tests for the 90-day open-position correlation risk metric.

Pure DB reads (no network, no yfinance monkeypatching needed): prices are
seeded directly into ``price_history`` via the shared ``db_session_factory``
fixture, following the seeding pattern used by ``test_overview.py`` and
``test_evaluation_pending.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

import app.engine.orchestrator as orchestrator
from app.engine.orchestrator import (
    _CORRELATION_ALERT_THRESHOLD,
    _build_risk_metrics,
    _compute_open_allocation,
    _daily_returns_by_session,
    _open_buy_positions,
    _open_position_correlations,
)
from app.engine.policy import MarketMetrics
from app.engine.sim_trader import sync_shadow_book
from app.models import AnalysisRun, PriceHistory, Recommendation, Symbol, UserTransaction

# A deterministic, oscillating (non-constant, non-trending) return series so
# correlation is well-defined (nonzero variance) and reproducible across runs.
_RETURN_FRACTIONS: list[float] = (
    [0.01, -0.02, 0.015, -0.005, 0.02, -0.01, 0.008, -0.015, 0.012, -0.007] * 10
)


def _prices_from_returns(returns: list[float], start: float = 100.0) -> list[float]:
    prices = [start]
    for r in returns:
        prices.append(prices[-1] * (1.0 + r))
    return prices


def _seed_symbol(
    db: Session,
    ticker: str,
    closes: list[float],
    *,
    ts_hour_offset_prev_day: int | None = None,
) -> int:
    """Insert a Symbol + one daily PriceHistory row per close, newest last (today).

    By default each row's ``ts`` is midnight UTC of its session day (a US-style
    exchange). Passing ``ts_hour_offset_prev_day=H`` instead places ``ts`` at
    H:00 UTC of the PRIOR calendar day, simulating a non-US exchange whose local
    midnight is stored as a late-UTC-hour timestamp on the previous day (e.g.
    Milan, CET/CEST) — the scenario ``_daily_returns_by_session`` must realign.
    """
    symbol = Symbol(ticker=ticker, name=f"{ticker} Inc.", exchange="NASDAQ", currency="USD")
    db.add(symbol)
    db.commit()
    db.refresh(symbol)

    n = len(closes)
    today_midnight = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    for i, close in enumerate(closes):
        session_midnight = today_midnight - timedelta(days=(n - 1 - i))
        if ts_hour_offset_prev_day is not None:
            ts = session_midnight - timedelta(days=1) + timedelta(hours=ts_hour_offset_prev_day)
        else:
            ts = session_midnight
        db.add(
            PriceHistory(
                symbol_id=symbol.id,
                ts=ts,
                interval="1d",
                open=close,
                high=close,
                low=close,
                close=close,
                volume=1_000_000.0,
            )
        )
    db.commit()
    return symbol.id


def _seed_buy(
    db: Session, symbol_id: int, *, allocation_pct: float = 10.0, days_ago: int = 10
) -> None:
    """Semina un BUY e materializza la posizione nel libro simulato.

    ``_open_buy_positions`` non deduce più le posizioni aperte dai consigli (era
    l'euristica senza scadenza che saturava la regola 10): la sorgente di verità
    è ora ``sim_positions``, quindi il consiglio da solo non basta e va fatta
    girare la passata del motore. Il BUY è datato indietro perché il riempimento
    avviene alla chiusura della seduta SUCCESSIVA, che deve esistere.
    """
    run = AnalysisRun(symbol_id=symbol_id, status="COMPLETED", trigger="MANUAL")
    db.add(run)
    db.flush()
    db.add(
        Recommendation(
            run_id=run.id,
            symbol_id=symbol_id,
            action="BUY",
            sizing_strategy="DCA",
            confidence=0.6,
            allocation_pct=allocation_pct,
            dca_tranches=1,
            horizon_days=60,
            validator_verdict="APPROVE",
            created_at=datetime.utcnow() - timedelta(days=days_ago),
        )
    )
    db.commit()
    sync_shadow_book(db)


def _seed_sell(db: Session, symbol_id: int, *, days_ago: int = 5) -> None:
    """Semina un SELL e lo fa eseguire, chiudendo la posizione."""
    run = AnalysisRun(symbol_id=symbol_id, status="COMPLETED", trigger="MANUAL")
    db.add(run)
    db.flush()
    db.add(
        Recommendation(
            run_id=run.id,
            symbol_id=symbol_id,
            action="SELL",
            sizing_strategy="WAIT",
            confidence=0.6,
            allocation_pct=0.0,
            validator_verdict="APPROVE",
            created_at=datetime.utcnow() - timedelta(days=days_ago),
        )
    )
    db.commit()
    sync_shadow_book(db)


# --------------------------------------------------------------------------- #
# 1 & 2. Perfectly correlated / anti-correlated pair with an open BUY
# --------------------------------------------------------------------------- #


def test_open_position_correlations_perfectly_correlated_pair(
    db_session_factory: sessionmaker[Session],
) -> None:
    db = db_session_factory()
    try:
        closes_a = _prices_from_returns(_RETURN_FRACTIONS)
        current_id = _seed_symbol(db, "CUR1", closes_a)
        # Same returns (scaled price level) -> exact same daily % changes.
        other_id = _seed_symbol(db, "OTH1", [c * 2.0 for c in closes_a])
        _seed_buy(db, other_id)

        df_current = orchestrator.market_data_service.get_history_df(
            db, current_id, interval="1d", days=180
        )
        results = _open_position_correlations(db, current_id, df_current)

        assert len(results) == 1
        assert results[0]["ticker"] == "OTH1"
        assert results[0]["correlation"] == pytest.approx(1.0, abs=0.01)
    finally:
        db.close()


def test_open_position_correlations_anti_correlated_pair(
    db_session_factory: sessionmaker[Session],
) -> None:
    db = db_session_factory()
    try:
        closes_a = _prices_from_returns(_RETURN_FRACTIONS)
        closes_b = _prices_from_returns([-r for r in _RETURN_FRACTIONS])
        current_id = _seed_symbol(db, "CUR2", closes_a)
        other_id = _seed_symbol(db, "OTH2", closes_b)
        _seed_buy(db, other_id)

        df_current = orchestrator.market_data_service.get_history_df(
            db, current_id, interval="1d", days=180
        )
        results = _open_position_correlations(db, current_id, df_current)

        assert len(results) == 1
        assert results[0]["correlation"] == pytest.approx(-1.0, abs=0.01)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 3. BUY followed by a later SELL -> no open position -> empty, None in risk_metrics
# --------------------------------------------------------------------------- #


def test_buy_then_sell_is_not_an_open_position(
    db_session_factory: sessionmaker[Session],
) -> None:
    db = db_session_factory()
    try:
        closes_a = _prices_from_returns(_RETURN_FRACTIONS)
        current_id = _seed_symbol(db, "CUR3", closes_a)
        other_id = _seed_symbol(db, "OTH3", closes_a)
        _seed_buy(db, other_id)
        _seed_sell(db, other_id)

        assert _open_buy_positions(db, current_id) == []

        df_current = orchestrator.market_data_service.get_history_df(
            db, current_id, interval="1d", days=180
        )
        results = _open_position_correlations(db, current_id, df_current)
        assert results == []

        metrics = MarketMetrics(last_close=100.0)
        risk_metrics = _build_risk_metrics(metrics, {}, {}, {}, results)
        assert risk_metrics["open_position_correlations_90d"] is None
        assert risk_metrics["max_open_position_correlation_90d"] is None
        assert risk_metrics["correlation_alert"] is False
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 4. Insufficient overlapping history -> pair dropped, never estimated
# --------------------------------------------------------------------------- #


def test_insufficient_overlap_drops_the_pair(
    db_session_factory: sessionmaker[Session],
) -> None:
    db = db_session_factory()
    try:
        closes_a = _prices_from_returns(_RETURN_FRACTIONS)  # ~101 sessions of history
        current_id = _seed_symbol(db, "CUR4", closes_a)
        # Only 20 sessions of history -> ~19 overlapping returns, well under
        # the _CORRELATION_MIN_OVERLAP=60 floor.
        short_closes = _prices_from_returns(_RETURN_FRACTIONS[:19])
        other_id = _seed_symbol(db, "OTH4", short_closes)
        _seed_buy(db, other_id)

        df_current = orchestrator.market_data_service.get_history_df(
            db, current_id, interval="1d", days=180
        )
        results = _open_position_correlations(db, current_id, df_current)

        assert results == []
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 5. open_allocation_pct comes from the simulated book, and a closed position
#    stops occupying allocation (il bug che saturava la regola 10)
# --------------------------------------------------------------------------- #


def test_only_open_positions_occupy_allocation(
    db_session_factory: sessionmaker[Session],
) -> None:
    db = db_session_factory()
    try:
        closes_a = _prices_from_returns(_RETURN_FRACTIONS)
        current_id = _seed_symbol(db, "CUR5", closes_a)

        open_id = _seed_symbol(db, "OPEN5", closes_a)
        _seed_buy(db, open_id, allocation_pct=12.5)

        closed_id = _seed_symbol(db, "CLOSED5", closes_a)
        _seed_buy(db, closed_id, allocation_pct=8.0)
        _seed_sell(db, closed_id)

        assert _compute_open_allocation(db, current_id) == pytest.approx(12.5)
    finally:
        db.close()


def test_an_expired_position_frees_allocation_for_new_buys(
    db_session_factory: sessionmaker[Session],
) -> None:
    """Il bug vero, al livello in cui mordeva.

    Con la vecchia euristica un BUY restava "aperto" per sempre, quindi
    ``open_allocation_pct`` cresceva in modo monotono: era arrivato al 70% e,
    sommato alla riserva di liquidità del 30%, la regola 10 forzava a HOLD ogni
    nuovo BUY (7 delle ultime 14 analisi in produzione). Ora una posizione scade
    e libera lo spazio che occupava.
    """
    db = db_session_factory()
    try:
        closes = _prices_from_returns(_RETURN_FRACTIONS)
        current_id = _seed_symbol(db, "CUR8", closes)
        old_id = _seed_symbol(db, "OLD8", closes)

        run = AnalysisRun(symbol_id=old_id, status="COMPLETED", trigger="MANUAL")
        db.add(run)
        db.flush()
        db.add(
            Recommendation(
                run_id=run.id,
                symbol_id=old_id,
                action="BUY",
                sizing_strategy="ALL_IN",
                confidence=0.6,
                allocation_pct=70.0,
                dca_tranches=1,
                # Orizzonte breve, consiglio vecchio: la scadenza è già passata.
                horizon_days=7,
                validator_verdict="APPROVE",
                created_at=datetime.utcnow() - timedelta(days=40),
            )
        )
        db.commit()
        sync_shadow_book(db)

        # La posizione è nata, ha vissuto ed è scaduta: non occupa più nulla.
        assert _compute_open_allocation(db, current_id) == pytest.approx(0.0)
        assert _open_buy_positions(db, current_id) == []
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 6. Cross-exchange timestamp offset (Milan-style) still aligns correctly
# --------------------------------------------------------------------------- #


def test_cross_exchange_timestamp_offset_still_aligns(
    db_session_factory: sessionmaker[Session],
) -> None:
    db = db_session_factory()
    try:
        closes_a = _prices_from_returns(_RETURN_FRACTIONS)
        # US-style: ts at plain midnight UTC (the default).
        current_id = _seed_symbol(db, "CURUS", closes_a)
        # Milan-style: same underlying sessions/returns, but ts stored at
        # 22:00 UTC of the PRIOR calendar day (simulating CET/CEST midnight).
        other_id = _seed_symbol(
            db, "OTHMI", [c * 3.0 for c in closes_a], ts_hour_offset_prev_day=22
        )
        _seed_buy(db, other_id)

        df_current = orchestrator.market_data_service.get_history_df(
            db, current_id, interval="1d", days=180
        )
        results = _open_position_correlations(db, current_id, df_current)

        assert len(results) == 1
        assert results[0]["correlation"] == pytest.approx(1.0, abs=0.01)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 7. correlation_alert flips at the threshold
# --------------------------------------------------------------------------- #


def test_correlation_alert_threshold() -> None:
    metrics = MarketMetrics(last_close=100.0)

    below = _build_risk_metrics(
        metrics, {}, {}, {}, [{"ticker": "X", "correlation": _CORRELATION_ALERT_THRESHOLD - 0.05}]
    )
    assert below["correlation_alert"] is False

    at_or_above = _build_risk_metrics(
        metrics, {}, {}, {}, [{"ticker": "X", "correlation": _CORRELATION_ALERT_THRESHOLD}]
    )
    assert at_or_above["correlation_alert"] is True
    assert at_or_above["max_open_position_correlation_90d"] == pytest.approx(
        _CORRELATION_ALERT_THRESHOLD
    )


# --------------------------------------------------------------------------- #
# 8. Union with real user paper-trading positions (blueprint §5.4 addendum)
# --------------------------------------------------------------------------- #


def _seed_user_buy(db: Session, symbol_id: int) -> None:
    db.add(
        UserTransaction(
            symbol_id=symbol_id, side="BUY", amount=500.0, fee_pct=0.0,
            currency="USD", executed_at=datetime.utcnow() - timedelta(days=1),
        )
    )
    db.commit()


def test_open_position_correlations_includes_user_only_position(
    db_session_factory: sessionmaker[Session],
) -> None:
    """A symbol with ONLY a logged user BUY (no system Recommendation at all)
    must still enter the correlation calc — the two "open position" sources
    are unioned, not just the system's own Recommendation trail."""
    db = db_session_factory()
    try:
        closes_a = _prices_from_returns(_RETURN_FRACTIONS)
        current_id = _seed_symbol(db, "CURU1", closes_a)
        user_only_id = _seed_symbol(db, "USRONLY1", [c * 2.0 for c in closes_a])
        _seed_user_buy(db, user_only_id)  # NO _seed_buy() -> no Recommendation at all

        df_current = orchestrator.market_data_service.get_history_df(
            db, current_id, interval="1d", days=180
        )
        results = _open_position_correlations(db, current_id, df_current)

        assert any(r["ticker"] == "USRONLY1" for r in results)
    finally:
        db.close()


def test_open_position_correlations_does_not_duplicate_symbol_in_both_sources(
    db_session_factory: sessionmaker[Session],
) -> None:
    """A symbol with BOTH a system Recommendation BUY and a user-logged BUY
    must appear exactly once in the results."""
    db = db_session_factory()
    try:
        closes_a = _prices_from_returns(_RETURN_FRACTIONS)
        current_id = _seed_symbol(db, "CURU2", closes_a)
        both_id = _seed_symbol(db, "BOTH1", [c * 2.0 for c in closes_a])
        _seed_buy(db, both_id)
        _seed_user_buy(db, both_id)

        df_current = orchestrator.market_data_service.get_history_df(
            db, current_id, interval="1d", days=180
        )
        results = _open_position_correlations(db, current_id, df_current)

        matches = [r for r in results if r["ticker"] == "BOTH1"]
        assert len(matches) == 1
    finally:
        db.close()
