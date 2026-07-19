"""Price history + technical indicators endpoint (blueprint section 4, row 6)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.data.indicators import compute_all
from app.data.market import MarketDataService
from app.models import PriceHistory, Symbol
from app.schemas import IndicatorSeries, PriceHistoryOut, PricePoint

logger = logging.getLogger(__name__)

router = APIRouter(tags=["prices"])

# A daily bar older than this is considered stale (accounts for weekends: a
# Friday close is still "current" on Saturday/Sunday). Intraday bars are
# considered stale after a much shorter window.
_STALE_THRESHOLD_1D = timedelta(days=2)
_STALE_THRESHOLD_1H = timedelta(hours=2)


async def _refresh_if_stale(db: Session, symbol: Symbol, interval: str) -> None:
    """Refresh prices from the market data provider if the latest bar is stale."""
    last_ts = db.execute(
        select(PriceHistory.ts)
        .where(PriceHistory.symbol_id == symbol.id, PriceHistory.interval == interval)
        .order_by(PriceHistory.ts.desc())
        .limit(1)
    ).scalar_one_or_none()

    threshold = _STALE_THRESHOLD_1D if interval == "1d" else _STALE_THRESHOLD_1H
    is_stale = last_ts is None or (datetime.utcnow() - last_ts) > threshold
    if not is_stale:
        return

    market_service = MarketDataService()
    refresh_days = 730 if interval == "1d" else 30
    try:
        await asyncio.to_thread(market_service.refresh_prices, db, symbol, interval, refresh_days)
    except Exception:
        # Serve whatever is already stored rather than fail the whole request
        # because the upstream provider is unreachable.
        logger.warning("Refresh prezzi fallito per %s (%s)", symbol.ticker, interval, exc_info=True)


@router.get("/symbols/{symbol_id}/prices", response_model=PriceHistoryOut)
async def get_prices(
    symbol_id: int,
    interval: str = Query("1d", pattern="^(1d|1h)$"),
    days: int = Query(180, ge=1, le=730),
    indicators: bool = Query(True),
    db: Session = Depends(get_db),
) -> PriceHistoryOut:
    """Return stored OHLCV history (refreshing first if stale) plus indicators."""
    symbol = db.get(Symbol, symbol_id)
    if symbol is None:
        raise HTTPException(status_code=404, detail="Simbolo non trovato.")

    await _refresh_if_stale(db, symbol, interval)

    cutoff = datetime.utcnow() - timedelta(days=days)
    # Indicators need warm-up BEFORE the requested window (SMA200 alone needs
    # 200 trading bars ≈ 290 calendar days): load an extended window, compute
    # on the whole frame, then slice the series back to the visible points —
    # otherwise long indicators (SMA200 in a 180-day chart) are always None.
    cutoff_extended = cutoff - timedelta(days=320)
    all_rows = (
        db.execute(
            select(PriceHistory)
            .where(
                PriceHistory.symbol_id == symbol_id,
                PriceHistory.interval == interval,
                PriceHistory.ts >= cutoff_extended,
            )
            .order_by(PriceHistory.ts.asc())
        )
        .scalars()
        .all()
    )
    rows = [r for r in all_rows if r.ts >= cutoff]

    points = [
        PricePoint(ts=r.ts, open=r.open, high=r.high, low=r.low, close=r.close, volume=r.volume)
        for r in rows
    ]

    indicator_series: IndicatorSeries | None = None
    if indicators and points:
        df = pd.DataFrame(
            {
                "open": [r.open for r in all_rows],
                "high": [r.high for r in all_rows],
                "low": [r.low for r in all_rows],
                "close": [r.close for r in all_rows],
                "volume": [r.volume for r in all_rows],
            },
            index=pd.to_datetime([r.ts for r in all_rows]),
        )
        computed = compute_all(df)
        visible = len(points)
        series_only = {
            k: (v[-visible:] if isinstance(v, list) else v)
            for k, v in computed.items()
            if k != "latest"
        }
        indicator_series = IndicatorSeries(**series_only)

    return PriceHistoryOut(
        ticker=symbol.ticker, interval=interval, points=points, indicators=indicator_series
    )
