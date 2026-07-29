"""Portafoglio simulato del sistema (blueprint §6 addendum).

Sola lettura: il libro viene scritto solo dal motore deterministico
(``app.engine.sim_trader``), mai da una chiamata HTTP. Non c'è alcun endpoint per
aprire o chiudere una posizione a mano, e non è una dimenticanza: il valore di
questo libro sta nell'essere interamente riproducibile dai consigli e dai prezzi
persistiti, e un intervento manuale distruggerebbe proprio quella proprietà. Il
diario dell'utente — modificabile — è un'altra cosa e vive in
``app.api.transactions``.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.engine.positions import resolve_session_close
from app.engine.sim_book import (
    SIM_BASE_NOTIONAL,
    SIM_FEE_PCT,
    aggregate_book,
    sim_stats,
)
from app.engine.sim_trader import closed_position_rows
from app.models import SimPosition, Symbol
from app.schemas import SimPortfolioOut

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sim"])

#: Quante posizioni chiuse restituire nello storico recente: basta a leggere
#: l'andamento senza spedire tutto il libro a ogni caricamento di pagina.
_MAX_RECENT_CLOSED = 20


def _position_row(db: Session, pos: SimPosition, today) -> dict:
    """Una posizione arricchita con la valutazione a mercato, se calcolabile."""
    symbol = db.get(Symbol, pos.symbol_id)
    market_value: float | None = None
    unrealized: float | None = None
    unrealized_pct: float | None = None
    if pos.status == "OPEN" and pos.shares_open:
        last_close = resolve_session_close(db, pos.symbol_id, today)
        if last_close is not None:
            market_value = float(last_close) * float(pos.shares_open)
            unrealized = market_value - float(pos.cost_total or 0.0)
            if pos.cost_total:
                unrealized_pct = unrealized / float(pos.cost_total) * 100.0

    return {
        "id": pos.id,
        "symbol_id": pos.symbol_id,
        "ticker": symbol.ticker if symbol else "?",
        "name": symbol.name if symbol else "",
        "currency": pos.currency,
        "status": pos.status,
        "weight_pct": pos.weight_pct,
        "avg_entry_price": pos.avg_entry_price,
        "cost_total": pos.cost_total,
        "shares_open": pos.shares_open,
        "opened_session": pos.opened_session,
        "closed_session": pos.closed_session,
        "stop_loss_price": pos.stop_loss_price,
        "take_profit_price": pos.take_profit_price,
        "horizon_days": pos.horizon_days,
        "close_reason": pos.close_reason,
        "exit_price": pos.exit_price,
        "realized_pnl": pos.realized_pnl,
        "realized_pnl_pct": pos.realized_pnl_pct,
        "exit_ambiguous": pos.exit_ambiguous,
        "mae_pct": pos.mae_pct,
        "mfe_pct": pos.mfe_pct,
        "market_value": market_value,
        "unrealized_pnl": unrealized,
        "unrealized_pnl_pct": unrealized_pct,
        "days_open": (
            (today - pos.opened_session).days if pos.opened_session and pos.status == "OPEN" else None
        ),
    }


@router.get("/sim/portfolio", response_model=SimPortfolioOut)
def get_sim_portfolio(
    db: Session = Depends(get_db),
    recent_closed: int = Query(_MAX_RECENT_CLOSED, ge=1, le=100),
) -> SimPortfolioOut:
    """Stato del libro simulato: posizioni, aggregati per valuta e statistiche.

    Gli aggregati monetari sono per valuta e mai sommati fra valute: l'app non ha
    uno strato di cambio e non inventa un tasso.
    """
    today = datetime.utcnow().date()

    live = (
        db.execute(
            select(SimPosition)
            .where(SimPosition.status.in_(("OPEN", "STALE")))
            .order_by(SimPosition.weight_pct.desc())
        )
        .scalars()
        .all()
    )
    closed = (
        db.execute(
            select(SimPosition)
            .where(SimPosition.status == "CLOSED")
            .order_by(SimPosition.closed_session.desc(), SimPosition.id.desc())
            .limit(recent_closed)
        )
        .scalars()
        .all()
    )

    live_rows = [_position_row(db, pos, today) for pos in live]
    closed_rows = [_position_row(db, pos, today) for pos in closed]

    # Gli aggregati usano TUTTE le posizioni chiuse, non solo le ultime venti
    # mostrate: un P&L cumulato troncato all'ultima pagina sarebbe sbagliato.
    all_closed_pnl = (
        db.execute(
            select(SimPosition.status, SimPosition.currency, SimPosition.weight_pct,
                   SimPosition.cost_total, SimPosition.realized_pnl)
            .where(SimPosition.status == "CLOSED")
        )
        .all()
    )
    aggregate_input = live_rows + [
        {
            "status": status,
            "currency": currency,
            "weight_pct": weight,
            "cost_total": cost,
            "realized_pnl": pnl,
            "unrealized_pnl": None,
        }
        for status, currency, weight, cost, pnl in all_closed_pnl
    ]
    aggregates = aggregate_book(aggregate_input)
    stats = sim_stats(closed_position_rows(db))

    return SimPortfolioOut(
        base_notional=SIM_BASE_NOTIONAL,
        fee_pct=SIM_FEE_PCT,
        n_open=aggregates["n_open"],
        n_closed=aggregates["n_closed"],
        n_stale=aggregates["n_stale"],
        gross_exposure_pct=aggregates["gross_exposure_pct"],
        by_currency=aggregates["by_currency"],
        open_positions=[row for row in live_rows if row["status"] == "OPEN"],
        stale_positions=[row for row in live_rows if row["status"] == "STALE"],
        recent_closed=closed_rows,
        stats=stats,
    )
