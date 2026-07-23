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


async def _refresh_if_stale(db: Session, symbol: Symbol, interval: str, days: int) -> None:
    """Refresh prices from the market data provider if needed for this request.

    Two independent reasons to refresh: the latest bar is stale (the existing
    check), OR the stored history doesn't reach back far enough to cover the
    requested ``days`` (e.g. the chart's "5 anni"/"10 anni"/"sempre" ranges —
    routine refreshes elsewhere in the app only ever fetch 730 days, so a
    longer chart range needs its own, deeper fetch here). When the symbol
    simply doesn't have that much real history (e.g. a recent IPO), this
    refetches the same data yfinance already gave us — a harmless no-op
    upsert, not worth caching a "no more history before X" marker for.
    """
    last_ts = db.execute(
        select(PriceHistory.ts)
        .where(PriceHistory.symbol_id == symbol.id, PriceHistory.interval == interval)
        .order_by(PriceHistory.ts.desc())
        .limit(1)
    ).scalar_one_or_none()
    first_ts = db.execute(
        select(PriceHistory.ts)
        .where(PriceHistory.symbol_id == symbol.id, PriceHistory.interval == interval)
        .order_by(PriceHistory.ts.asc())
        .limit(1)
    ).scalar_one_or_none()

    threshold = _STALE_THRESHOLD_1D if interval == "1d" else _STALE_THRESHOLD_1H
    is_stale = last_ts is None or (datetime.utcnow() - last_ts) > threshold
    requested_cutoff = datetime.utcnow() - timedelta(days=days)
    lacks_depth = first_ts is None or first_ts > requested_cutoff
    if not is_stale and not lacks_depth:
        return

    market_service = MarketDataService()
    refresh_days = max(days, 730) if interval == "1d" else 30
    try:
        await asyncio.to_thread(market_service.refresh_prices, db, symbol, interval, refresh_days)
    except Exception:
        # Serve whatever is already stored rather than fail the whole request
        # because the upstream provider is unreachable.
        logger.warning("Refresh prezzi fallito per %s (%s)", symbol.ticker, interval, exc_info=True)


#: Sentinel for the chart's "sempre" (all-time) range: yfinance simply returns
#: whatever history it actually has when asked to start 100 years back, so no
#: separate "max"/no-lower-bound code path is needed.
ALL_TIME_DAYS = 36_500


@router.get("/symbols/{symbol_id}/prices", response_model=PriceHistoryOut)
async def get_prices(
    symbol_id: int,
    interval: str = Query("1d", pattern="^(1d|1h)$"),
    days: int = Query(180, ge=1, le=ALL_TIME_DAYS),
    indicators: bool = Query(True),
    db: Session = Depends(get_db),
) -> PriceHistoryOut:
    """Return stored OHLCV history (refreshing first if needed) plus indicators."""
    symbol = db.get(Symbol, symbol_id)
    if symbol is None:
        raise HTTPException(status_code=404, detail="Simbolo non trovato.")

    await _refresh_if_stale(db, symbol, interval, days)

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
