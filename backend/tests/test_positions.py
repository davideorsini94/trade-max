"""Offline tests for the fictitious paper-trading position engine
(blueprint §5.4 addendum). Pure-function tests for ``summarize_position``/
``compact_position_for_prompt`` (no DB), plus DB tests for
``resolve_session_close``/``user_open_symbol_ids`` (seeded PriceHistory,
no network).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.engine.positions import (
    compact_position_for_prompt,
    resolve_session_close,
    summarize_position,
    user_open_symbol_ids,
)
from app.models import PriceHistory, Symbol, UserTransaction


def _tx(
    side: str,
    amount: float,
    *,
    fee_pct: float = 0.0,
    price_ref: float | None = None,
    quantity_est: float | None = None,
    executed_at: datetime | None = None,
    currency: str = "USD",
) -> dict:
    return {
        "side": side,
        "amount": amount,
        "fee_pct": fee_pct,
        "currency": currency,
        "executed_at": executed_at or datetime(2026, 1, 1),
        "price_ref": price_ref,
        "quantity_est": quantity_est,
    }


# --------------------------------------------------------------------------- #
# summarize_position — core formulas
# --------------------------------------------------------------------------- #


def test_summarize_position_empty_is_none():
    assert summarize_position([], last_close=100.0) is None


def test_summarize_position_single_buy_with_fee():
    # 1000 invested, 1% fee, close 190 -> net capital 990, qty 990/190.
    qty = 990.0 / 190.0
    txs = [_tx("BUY", 1000.0, fee_pct=1.0, price_ref=190.0, quantity_est=qty)]
    summary = summarize_position(txs, last_close=200.0)

    assert summary["status"] == "OPEN"
    assert summary["invested_total"] == pytest.approx(1000.0)
    assert summary["proceeds_net"] == pytest.approx(0.0)
    assert summary["realized_cashflow"] == pytest.approx(-1000.0)
    assert summary["estimates_complete"] is True
    assert summary["est_shares_open"] == pytest.approx(qty)
    assert summary["avg_cost_est"] == pytest.approx(1000.0 / qty)
    assert summary["current_value_est"] == pytest.approx(qty * 200.0)
    expected_pnl = -1000.0 + qty * 200.0
    assert summary["total_pnl_est"] == pytest.approx(expected_pnl, abs=1e-3)
    assert summary["total_pnl_pct_est"] == pytest.approx(expected_pnl / 1000.0 * 100.0, abs=1e-3)


def test_summarize_position_fully_closed_by_matching_sell():
    buy_qty = 1000.0 / 190.0
    txs = [
        _tx("BUY", 1000.0, price_ref=190.0, quantity_est=buy_qty, executed_at=datetime(2026, 1, 1)),
        _tx("SELL", buy_qty * 210.0, fee_pct=1.0, price_ref=210.0, quantity_est=buy_qty, executed_at=datetime(2026, 2, 1)),
    ]
    summary = summarize_position(txs, last_close=250.0)

    assert summary["status"] == "CLOSED"
    net_proceeds = buy_qty * 210.0 * 0.99
    expected_cashflow = net_proceeds - 1000.0
    assert summary["realized_cashflow"] == pytest.approx(expected_cashflow)
    # Closed position: shares open ~0, so current_value_est contributes ~0.
    assert summary["total_pnl_est"] == pytest.approx(expected_cashflow, abs=1e-3)


def test_summarize_position_incomplete_estimates_degrade_cleanly():
    # One BUY has no resolvable price_ref -> quantity_est is None for it.
    txs = [
        _tx("BUY", 500.0, price_ref=None, quantity_est=None, executed_at=datetime(2020, 1, 1)),
        _tx("BUY", 500.0, price_ref=100.0, quantity_est=5.0, executed_at=datetime(2026, 1, 1)),
    ]
    summary = summarize_position(txs, last_close=120.0)

    assert summary["estimates_complete"] is False
    assert summary["status"] == "UNKNOWN"
    assert summary["est_shares_open"] is None
    assert summary["avg_cost_est"] is None
    assert summary["current_value_est"] is None
    assert summary["total_pnl_est"] is None
    assert summary["total_pnl_pct_est"] is None
    # Cashflow is still fully knowable even without share estimates.
    assert summary["invested_total"] == pytest.approx(1000.0)
    assert summary["realized_cashflow"] == pytest.approx(-1000.0)


def test_summarize_position_no_last_close_still_computes_avg_cost():
    txs = [_tx("BUY", 1000.0, price_ref=200.0, quantity_est=5.0)]
    summary = summarize_position(txs, last_close=None)

    assert summary["avg_cost_est"] == pytest.approx(200.0)
    assert summary["current_value_est"] is None
    assert summary["total_pnl_est"] is None


# --------------------------------------------------------------------------- #
# compact_position_for_prompt
# --------------------------------------------------------------------------- #


def test_compact_position_for_prompt_none_passthrough():
    assert compact_position_for_prompt(None) is None


def test_compact_position_for_prompt_drops_none_keys():
    summary = summarize_position(
        [_tx("BUY", 500.0, price_ref=None, quantity_est=None)], last_close=None
    )
    compact = compact_position_for_prompt(summary)
    assert compact is not None
    assert "est_shares" not in compact
    assert "avg_cost_est" not in compact
    assert compact["status"] == "UNKNOWN"
    assert compact["invested"] == pytest.approx(500.0)


def test_compact_position_for_prompt_includes_first_buy_when_present():
    summary = summarize_position(
        [_tx("BUY", 500.0, price_ref=100.0, quantity_est=5.0, executed_at=datetime(2026, 5, 12))],
        last_close=110.0,
    )
    compact = compact_position_for_prompt(summary)
    assert compact["first_buy"] == "2026-05-12"
    assert compact["est_shares"] == pytest.approx(5.0)


# --------------------------------------------------------------------------- #
# resolve_session_close (DB-backed, no network)
# --------------------------------------------------------------------------- #


def _seed_symbol_with_closes(
    factory: sessionmaker[Session], ticker: str, closes: dict[str, float]
) -> int:
    """``closes`` maps 'YYYY-MM-DD' -> close; each becomes a daily bar at midnight UTC."""
    session = factory()
    try:
        symbol = Symbol(ticker=ticker, name=f"{ticker} Inc.", exchange="NASDAQ", currency="USD")
        session.add(symbol)
        session.commit()
        session.refresh(symbol)
        for day_str, close in closes.items():
            y, m, d = (int(x) for x in day_str.split("-"))
            session.add(
                PriceHistory(
                    symbol_id=symbol.id,
                    ts=datetime(y, m, d),
                    interval="1d",
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                    volume=1_000_000.0,
                )
            )
        session.commit()
        return symbol.id
    finally:
        session.close()


def test_resolve_session_close_exact_day(db_session_factory: sessionmaker[Session]) -> None:
    symbol_id = _seed_symbol_with_closes(db_session_factory, "RSC1", {"2026-03-10": 150.0})
    db = db_session_factory()
    try:
        assert resolve_session_close(db, symbol_id, date(2026, 3, 10)) == pytest.approx(150.0)
    finally:
        db.close()


def test_resolve_session_close_weekend_falls_back_to_friday(
    db_session_factory: sessionmaker[Session],
) -> None:
    # 2026-03-13 is a Friday; 2026-03-14/15 are Sat/Sun with no bars.
    symbol_id = _seed_symbol_with_closes(db_session_factory, "RSC2", {"2026-03-13": 88.0})
    db = db_session_factory()
    try:
        assert resolve_session_close(db, symbol_id, date(2026, 3, 15)) == pytest.approx(88.0)
    finally:
        db.close()


def test_resolve_session_close_none_when_no_bar_within_5_days(
    db_session_factory: sessionmaker[Session],
) -> None:
    symbol_id = _seed_symbol_with_closes(db_session_factory, "RSC3", {"2026-01-01": 50.0})
    db = db_session_factory()
    try:
        assert resolve_session_close(db, symbol_id, date(2026, 3, 1)) is None
    finally:
        db.close()


def test_resolve_session_close_non_us_ts_offset_handled(
    db_session_factory: sessionmaker[Session],
) -> None:
    # Milan-style: ts stored at 22:00 UTC of the PRIOR day for session 2026-04-02.
    session = db_session_factory()
    try:
        symbol = Symbol(ticker="MIL1", name="Milan Co", exchange="MIL", currency="EUR")
        session.add(symbol)
        session.commit()
        session.refresh(symbol)
        session.add(
            PriceHistory(
                symbol_id=symbol.id,
                ts=datetime(2026, 4, 1, 22, 0),  # session date is 2026-04-02 after +12h
                interval="1d",
                open=10.0, high=10.0, low=10.0, close=42.0, volume=1.0,
            )
        )
        session.commit()
        symbol_id = symbol.id
    finally:
        session.close()

    db = db_session_factory()
    try:
        assert resolve_session_close(db, symbol_id, date(2026, 4, 2)) == pytest.approx(42.0)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# user_open_symbol_ids
# --------------------------------------------------------------------------- #


def test_user_open_symbol_ids_open_vs_closed(db_session_factory: sessionmaker[Session]) -> None:
    session = db_session_factory()
    try:
        open_sym = Symbol(ticker="OPEN1", name="Open Co", exchange="NASDAQ", currency="USD")
        closed_sym = Symbol(ticker="CLOSED1", name="Closed Co", exchange="NASDAQ", currency="USD")
        excluded_sym = Symbol(ticker="EXCL1", name="Excluded", exchange="NASDAQ", currency="USD")
        session.add_all([open_sym, closed_sym, excluded_sym])
        session.commit()
        for s in (open_sym, closed_sym, excluded_sym):
            session.refresh(s)

        session.add(
            UserTransaction(
                symbol_id=open_sym.id, side="BUY", amount=100.0, fee_pct=0.0,
                currency="USD", executed_at=datetime(2026, 1, 1),
            )
        )
        session.add(
            UserTransaction(
                symbol_id=closed_sym.id, side="BUY", amount=100.0, fee_pct=0.0,
                currency="USD", executed_at=datetime(2026, 1, 1),
            )
        )
        session.add(
            UserTransaction(
                symbol_id=closed_sym.id, side="SELL", amount=110.0, fee_pct=0.0,
                currency="USD", executed_at=datetime(2026, 2, 1),
            )
        )
        # Excluded symbol also has an open BUY, but it's the one we exclude.
        session.add(
            UserTransaction(
                symbol_id=excluded_sym.id, side="BUY", amount=100.0, fee_pct=0.0,
                currency="USD", executed_at=datetime(2026, 1, 1),
            )
        )
        session.commit()
        ids = (open_sym.id, closed_sym.id, excluded_sym.id)
    finally:
        session.close()

    db = db_session_factory()
    try:
        open_ids = user_open_symbol_ids(db, exclude_symbol_id=ids[2])
        assert ids[0] in open_ids
        assert ids[1] not in open_ids
        assert ids[2] not in open_ids  # excluded regardless of its own status
    finally:
        db.close()
