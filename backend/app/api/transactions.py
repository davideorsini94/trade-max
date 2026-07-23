"""Fictitious paper-trading transaction endpoints (blueprint §5.4 addendum).

Registers a BUY/SELL diary entry per symbol (amount, fee %, date — nothing
else is ever asked) and derives an aggregate position (estimated shares, cost
basis, mark-to-market P&L) from it. No real order is ever executed; see
``app.models.UserTransaction`` and ``app.engine.positions`` for the formulas.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.data.market import MarketDataService
from app.engine.positions import resolve_session_close, summarize_position
from app.models import PriceHistory, Symbol, UserTransaction
from app.schemas import PositionSummaryOut, TransactionCreate, TransactionListOut, TransactionOut

logger = logging.getLogger(__name__)

router = APIRouter(tags=["transactions"])

# A transaction dated further back than the routine 730-day refresh can reach
# gets one on-demand deep fetch attempt at this depth before giving up on a
# price_ref (mirrors app.api.prices's extended-range refresh).
_DEEP_REFRESH_DAYS = 3650


def _tx_to_dict(tx: UserTransaction) -> dict:
    """Plain dict for the pure ``app.engine.positions`` functions (no ORM)."""
    return {
        "side": tx.side,
        "amount": tx.amount,
        "fee_pct": tx.fee_pct,
        "currency": tx.currency,
        "executed_at": tx.executed_at,
        "price_ref": tx.price_ref,
        "quantity_est": tx.quantity_est,
    }


def _latest_close(db: Session, symbol_id: int) -> float | None:
    """Latest stored daily close for a symbol — DB-only, no network."""
    row = db.execute(
        select(PriceHistory.close)
        .where(PriceHistory.symbol_id == symbol_id, PriceHistory.interval == "1d")
        .order_by(PriceHistory.ts.desc())
        .limit(1)
    ).scalar_one_or_none()
    return float(row) if row is not None else None


@router.post(
    "/symbols/{symbol_id}/transactions", response_model=TransactionOut, status_code=201
)
async def create_transaction(
    symbol_id: int, body: TransactionCreate, db: Session = Depends(get_db)
) -> TransactionOut:
    """Register one fictitious BUY/SELL for a symbol.

    Never executes a real order. Estimates the share count from the session
    close on ``executed_at`` (deep-fetching history on demand if the date
    predates what's already stored); when no close can be resolved at all,
    the transaction is still saved with ``price_ref``/``quantity_est`` both
    null rather than a guessed value.
    """
    symbol = db.get(Symbol, symbol_id)
    if symbol is None:
        raise HTTPException(status_code=404, detail="Simbolo non trovato.")

    if body.side == "SELL":
        has_prior_buy = db.execute(
            select(UserTransaction.id).where(
                UserTransaction.symbol_id == symbol_id,
                UserTransaction.side == "BUY",
                UserTransaction.executed_at <= datetime.combine(body.executed_at, datetime.min.time()),
            )
        ).first()
        if has_prior_buy is None:
            raise HTTPException(
                status_code=422,
                detail="Non puoi registrare una vendita prima di alcun acquisto per questo titolo.",
            )

    price_ref = resolve_session_close(db, symbol_id, body.executed_at)
    if price_ref is None:
        try:
            market_service = MarketDataService()
            await asyncio.to_thread(
                market_service.refresh_prices, db, symbol, "1d", _DEEP_REFRESH_DAYS
            )
        except Exception:
            logger.warning(
                "Deep-fetch prezzi fallito per %s (transazione %s)",
                symbol.ticker,
                body.executed_at,
                exc_info=True,
            )
        price_ref = resolve_session_close(db, symbol_id, body.executed_at)

    quantity_est: float | None = None
    if price_ref is not None and price_ref > 0:
        net_capital = (
            body.amount * (1.0 - body.fee_pct / 100.0) if body.side == "BUY" else body.amount
        )
        quantity_est = net_capital / price_ref

    tx = UserTransaction(
        symbol_id=symbol_id,
        side=body.side,
        amount=body.amount,
        fee_pct=body.fee_pct,
        currency=symbol.currency,
        executed_at=datetime.combine(body.executed_at, datetime.min.time()),
        price_ref=price_ref,
        quantity_est=quantity_est,
        note=body.note,
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)
    return TransactionOut.model_validate(tx)


@router.get("/symbols/{symbol_id}/transactions", response_model=TransactionListOut)
def list_transactions(symbol_id: int, db: Session = Depends(get_db)) -> TransactionListOut:
    """All transactions for a symbol (newest first) plus the derived position.

    Read-only and network-free: ``last_close`` is whatever daily close is
    already stored, never a fresh fetch (that only happens on a symbol's own
    detail-page price refresh or when a new transaction is posted).
    """
    symbol = db.get(Symbol, symbol_id)
    if symbol is None:
        raise HTTPException(status_code=404, detail="Simbolo non trovato.")

    rows = (
        db.execute(
            select(UserTransaction)
            .where(UserTransaction.symbol_id == symbol_id)
            .order_by(UserTransaction.executed_at.desc(), UserTransaction.id.desc())
        )
        .scalars()
        .all()
    )

    last_close = _latest_close(db, symbol_id)
    summary = summarize_position([_tx_to_dict(tx) for tx in rows], last_close)

    return TransactionListOut(
        items=[TransactionOut.model_validate(tx) for tx in rows],
        position=PositionSummaryOut(**summary) if summary is not None else None,
    )


@router.delete("/transactions/{transaction_id}", status_code=204, response_model=None)
def delete_transaction(transaction_id: int, db: Session = Depends(get_db)) -> None:
    """Delete one transaction (to fix a mistaken entry).

    No PUT/edit endpoint on purpose: delete-and-reinsert covers corrections
    with far less code than recomputing estimates in place.
    """
    tx = db.get(UserTransaction, transaction_id)
    if tx is None:
        raise HTTPException(status_code=404, detail="Transazione non trovata.")
    db.delete(tx)
    db.commit()
