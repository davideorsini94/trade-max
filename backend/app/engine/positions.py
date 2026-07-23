"""Fictitious paper-trading positions, derived from ``UserTransaction`` rows.

Nothing here executes a real order: this module turns the user's own
money-in/money-out declarations (amount, fee %, date) into an estimated share
count, cost basis and mark-to-market P&L, so the pipeline can reason about the
user's REAL entry point instead of generic "if you already own it" text
(blueprint §5.4 addendum — advice_holder_it).

Design notes
------------
* Quantities are never asked from the user, only estimated from the session
  close on the transaction date (already in ``price_history``) — labelled
  "_est" everywhere so the UI never presents a guess as a fact.
* When a session close cannot be resolved for a transaction, its quantity
  estimate is ``None`` and the WHOLE position's ``estimates_complete`` flips
  to ``False``: partial share counts would silently understate/overstate the
  real position, so the summary refuses to guess a total from incomplete data.
* Amounts are always in the symbol's own currency (see ``UserTransaction``
  docstring in ``app.models``) — there is no FX layer in this app, so no
  conversion is ever attempted.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PriceHistory, UserTransaction

#: How many calendar days to look backward for a session close before giving
#: up (covers weekends and a short run of public holidays).
_CLOSE_LOOKBACK_DAYS = 5

#: A position is considered CLOSED once the estimated open shares fall to (or
#: below) this fraction of total shares ever bought — a tolerance for the
#: rounding noise inherent in estimating shares from prices.
_CLOSED_TOLERANCE_FRACTION = 0.001
_CLOSED_TOLERANCE_FLOOR = 1e-9


def resolve_session_close(db: Session, symbol_id: int, executed_date: date) -> float | None:
    """Daily close at-or-before ``executed_date``, or None if none within 5 days.

    Looks backward (not forward, unlike the evaluator's "realized return"
    lookups): a transaction dated on a weekend/holiday should resolve to the
    most recent PRIOR trading session, matching how a real order would have
    filled. ``price_history.ts`` is midnight *local exchange time* stored as
    naive UTC, so — mirroring the +12h session-date normalization already
    used for cross-exchange correlation in ``app.engine.orchestrator``
    (``_daily_returns_by_session``) — each row's session date is computed as
    ``(ts + 12h).date()`` before comparing to ``executed_date``.
    """
    cutoff_start = datetime.combine(
        executed_date - timedelta(days=_CLOSE_LOOKBACK_DAYS), datetime.min.time()
    )
    cutoff_end = datetime.combine(executed_date, datetime.min.time()) + timedelta(days=1)
    rows = (
        db.execute(
            select(PriceHistory.ts, PriceHistory.close)
            .where(
                PriceHistory.symbol_id == symbol_id,
                PriceHistory.interval == "1d",
                PriceHistory.ts >= cutoff_start,
                PriceHistory.ts < cutoff_end,
            )
            .order_by(PriceHistory.ts.desc())
        )
        .all()
    )
    for ts, close in rows:
        session_date = (ts + timedelta(hours=12)).date()
        if session_date <= executed_date and close is not None:
            return float(close)
    return None


def _estimate_quantity(side: str, amount: float, fee_pct: float, price_ref: float | None) -> float | None:
    """Estimated shares for one transaction, or None when ``price_ref`` is None.

    BUY: the fee is lost up front (it never buys shares), so only the net
    capital (``amount * (1 - fee_pct/100)``) is divided by the price. SELL:
    ``amount`` IS the gross proceeds of selling the shares, so the shares sold
    are simply ``amount / price_ref`` (the fee only reduces the net cash the
    user receives, computed separately, never the share count).
    """
    if price_ref is None or price_ref <= 0:
        return None
    if side == "BUY":
        net_capital = amount * (1.0 - fee_pct / 100.0)
        return net_capital / price_ref
    return amount / price_ref


def summarize_position(transactions: list[dict[str, Any]], last_close: float | None) -> dict[str, Any] | None:
    """Aggregate a symbol's transactions into one position summary, or None if empty.

    ``transactions`` are plain dicts shaped like ``UserTransaction`` columns
    (``side``, ``amount``, ``fee_pct``, ``price_ref``, ``quantity_est``,
    ``executed_at``) — never ORM rows, so this stays a pure function callable
    from both the API layer and the orchestrator without a live session.
    See the module-level "Design notes" for the exact formulas.
    """
    if not transactions:
        return None

    currency = transactions[0].get("currency") or ""
    invested_total = 0.0  # Sigma BUY cash out, fee included
    proceeds_net = 0.0  # Sigma SELL net proceeds (fee subtracted)
    buy_qty_total = 0.0
    sell_qty_total = 0.0
    estimates_complete = True
    first_buy_at: datetime | None = None
    last_tx_at: datetime | None = None

    for tx in transactions:
        side = tx.get("side")
        amount = float(tx.get("amount") or 0.0)
        fee_pct = float(tx.get("fee_pct") or 0.0)
        quantity_est = tx.get("quantity_est")
        executed_at = tx.get("executed_at")

        if side == "BUY":
            invested_total += amount
            if first_buy_at is None or (executed_at is not None and executed_at < first_buy_at):
                first_buy_at = executed_at
            if quantity_est is None:
                estimates_complete = False
            else:
                buy_qty_total += float(quantity_est)
        elif side == "SELL":
            proceeds_net += amount * (1.0 - fee_pct / 100.0)
            if quantity_est is None:
                estimates_complete = False
            else:
                sell_qty_total += float(quantity_est)

        if last_tx_at is None or (executed_at is not None and executed_at > last_tx_at):
            last_tx_at = executed_at

    realized_cashflow = proceeds_net - invested_total

    est_shares_open: float | None = None
    avg_cost_est: float | None = None
    current_value_est: float | None = None
    total_pnl_est: float | None = None
    total_pnl_pct_est: float | None = None

    if estimates_complete:
        est_shares_open = buy_qty_total - sell_qty_total
        if buy_qty_total > 0:
            avg_cost_est = invested_total / buy_qty_total
        if last_close is not None and est_shares_open is not None:
            current_value_est = max(est_shares_open, 0.0) * last_close
            total_pnl_est = realized_cashflow + current_value_est
            if invested_total > 0:
                total_pnl_pct_est = total_pnl_est / invested_total * 100.0

    closed_tolerance = max(_CLOSED_TOLERANCE_FLOOR, _CLOSED_TOLERANCE_FRACTION * buy_qty_total)
    if not estimates_complete:
        status = "UNKNOWN"
    elif est_shares_open is not None and est_shares_open <= closed_tolerance:
        status = "CLOSED"
    else:
        status = "OPEN"

    return {
        "status": status,
        "currency": currency,
        "n_transactions": len(transactions),
        "invested_total": round(invested_total, 4),
        "proceeds_net": round(proceeds_net, 4),
        "realized_cashflow": round(realized_cashflow, 4),
        "estimates_complete": estimates_complete,
        "est_shares_open": round(est_shares_open, 6) if est_shares_open is not None else None,
        "avg_cost_est": round(avg_cost_est, 4) if avg_cost_est is not None else None,
        "last_close": last_close,
        "current_value_est": round(current_value_est, 4) if current_value_est is not None else None,
        "total_pnl_est": round(total_pnl_est, 4) if total_pnl_est is not None else None,
        "total_pnl_pct_est": round(total_pnl_pct_est, 4) if total_pnl_pct_est is not None else None,
        "first_buy_at": first_buy_at,
        "last_tx_at": last_tx_at,
    }


def compact_position_for_prompt(summary: dict[str, Any] | None) -> dict[str, Any] | None:
    """Compact ``summarize_position`` output for an LLM prompt (~40-70 tokens).

    None keys are dropped (not sent as null) to save tokens; returns None
    unchanged so callers can pass it straight through as "no real position".
    """
    if summary is None:
        return None
    compact = {
        "status": summary["status"],
        "ccy": summary["currency"],
        "n_tx": summary["n_transactions"],
        "invested": summary["invested_total"],
        "received_net": summary["proceeds_net"],
        "realized_cashflow": summary["realized_cashflow"],
        "est_shares": summary["est_shares_open"],
        "avg_cost_est": summary["avg_cost_est"],
        "current_value_est": summary["current_value_est"],
        "pnl_est": summary["total_pnl_est"],
        "pnl_pct_est": summary["total_pnl_pct_est"],
        "first_buy": summary["first_buy_at"].date().isoformat() if summary["first_buy_at"] else None,
    }
    return {k: v for k, v in compact.items() if v is not None}


def user_open_symbol_ids(db: Session, exclude_symbol_id: int) -> set[int]:
    """Symbol ids (excluding ``exclude_symbol_id``) with a real OPEN user position.

    Used to widen the validator's concentration-risk correlation check beyond
    the system's own Recommendation trail (see
    ``app.engine.orchestrator._open_position_correlations``) to symbols the
    user has genuinely logged a paper-trading BUY for. Reads only
    ``symbol_id``/``side``/``executed_at`` — no price lookups, so this never
    touches the network.
    """
    tx_rows = (
        db.execute(
            select(UserTransaction.symbol_id, UserTransaction.side, UserTransaction.executed_at)
            .where(UserTransaction.symbol_id != exclude_symbol_id)
            .order_by(UserTransaction.symbol_id, UserTransaction.executed_at.asc())
        )
        .all()
    )
    by_symbol: dict[int, list[tuple[str, datetime]]] = {}
    for symbol_id, side, executed_at in tx_rows:
        by_symbol.setdefault(symbol_id, []).append((side, executed_at))

    open_ids: set[int] = set()
    for symbol_id, txs in by_symbol.items():
        buy_qty = sum(1 for side, _ in txs if side == "BUY")
        sell_qty = sum(1 for side, _ in txs if side == "SELL")
        if buy_qty > sell_qty:
            open_ids.add(symbol_id)
    return open_ids
