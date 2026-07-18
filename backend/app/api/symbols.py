"""Symbol management endpoints (blueprint section 4, rows 1-5).

Covers listing monitored symbols (with optional lightweight quotes), search
against market data providers, adding a new symbol (which validates the
ticker and seeds two years of daily history), deleting a symbol and toggling
its favorite status.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.data.market import MarketDataService
from app.models import PriceHistory, Recommendation, Symbol
from app.schemas import (
    FavoriteToggle,
    RecommendationBrief,
    SymbolCreate,
    SymbolOut,
    SymbolOverviewOut,
    SymbolSearchResult,
    SymbolWithQuote,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["symbols"])

# A daily bar older than this is considered stale (accounts for weekends), the
# same rule the prices endpoint uses in app.api.prices.
_STALE_THRESHOLD_1D = timedelta(days=2)

# In-memory fundamentals cache: {ticker: (data, fetched_at)}. yfinance ``.info``
# is a slow network call, so the overview endpoint reuses a recent snapshot
# rather than re-fetching on every 60s poll.
_FUNDAMENTALS_CACHE_TTL = timedelta(minutes=15)
_fundamentals_cache: dict[str, tuple[dict, datetime]] = {}


def _profile_get(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a provider profile that may be a dict or an object.

    ``MarketDataService`` results are not part of this module's contract, so
    we accept either shape defensively rather than assuming one.
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def build_symbol_with_quote(db: Session, symbol: Symbol, with_quotes: bool) -> SymbolWithQuote:
    """Build a ``SymbolWithQuote`` from an ORM row, optionally attaching a quote.

    Shared with ``app.api.dashboard`` so the dashboard summary and the plain
    symbol list stay consistent about what "last price / change / last
    recommendation" mean.
    """
    out = SymbolWithQuote.model_validate(symbol)
    if not with_quotes:
        return out

    last_two = (
        db.execute(
            select(PriceHistory)
            .where(PriceHistory.symbol_id == symbol.id, PriceHistory.interval == "1d")
            .order_by(PriceHistory.ts.desc())
            .limit(2)
        )
        .scalars()
        .all()
    )
    if last_two:
        out.last_price = last_two[0].close
        if len(last_two) == 2 and last_two[1].close:
            out.change_pct_1d = (last_two[0].close - last_two[1].close) / last_two[1].close * 100

    reco = (
        db.execute(
            select(Recommendation)
            .where(Recommendation.symbol_id == symbol.id)
            .order_by(Recommendation.created_at.desc())
            .limit(1)
        )
        .scalar_one_or_none()
    )
    if reco is not None:
        out.last_recommendation = RecommendationBrief.model_validate(reco)
    return out


@router.get("/symbols", response_model=list[SymbolWithQuote])
def list_symbols(
    favorites_only: bool = Query(False),
    with_quotes: bool = Query(False),
    db: Session = Depends(get_db),
) -> list[SymbolWithQuote]:
    """List monitored symbols, favorites first."""
    stmt = select(Symbol).where(Symbol.is_active.is_(True))
    if favorites_only:
        stmt = stmt.where(Symbol.is_favorite.is_(True))
    rows = db.execute(stmt).scalars().all()

    def sort_key(s: Symbol) -> tuple[int, Any]:
        if s.is_favorite:
            return (0, s.favorite_added_at or datetime.min)
        return (1, s.ticker)

    rows = sorted(rows, key=sort_key)
    return [build_symbol_with_quote(db, row, with_quotes) for row in rows]


@router.get("/symbols/search", response_model=list[SymbolSearchResult])
async def search_symbols(
    q: str = Query(..., min_length=2),
    db: Session = Depends(get_db),
) -> list[SymbolSearchResult]:
    """Search for tickers via the market data provider (yfinance / Yahoo fallback)."""
    market_service = MarketDataService()
    try:
        raw_results = await asyncio.to_thread(market_service.search, q.strip())
    except Exception:
        logger.warning("Ricerca simboli fallita per query %r", q, exc_info=True)
        raw_results = []

    existing_tickers = {
        row[0].upper() for row in db.execute(select(Symbol.ticker)).all()
    }

    results: list[SymbolSearchResult] = []
    for item in raw_results or []:
        ticker = str(_profile_get(item, "ticker", "") or "").strip().upper()
        if not ticker:
            continue
        results.append(
            SymbolSearchResult(
                ticker=ticker,
                name=str(_profile_get(item, "name", "") or ""),
                exchange=_profile_get(item, "exchange", None),
                asset_type=str(_profile_get(item, "asset_type", "EQUITY") or "EQUITY"),
                already_added=ticker in existing_tickers,
            )
        )
    return results


@router.post("/symbols", response_model=SymbolOut, status_code=status.HTTP_201_CREATED)
async def create_symbol(payload: SymbolCreate, db: Session = Depends(get_db)) -> SymbolOut:
    """Add a new symbol: validates the ticker, then seeds 2 years of daily history."""
    ticker = payload.ticker.strip().upper()
    if not ticker:
        raise HTTPException(status_code=400, detail="Ticker non valido.")

    existing = db.execute(select(Symbol).where(Symbol.ticker == ticker)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Simbolo già monitorato.")

    market_service = MarketDataService()
    try:
        profile = await asyncio.to_thread(market_service.validate_and_profile, ticker)
    except Exception as exc:
        logger.error("Validazione ticker %s fallita: %s", ticker, exc, exc_info=True)
        raise HTTPException(
            status_code=502, detail="Servizio dati di mercato non disponibile. Riprova più tardi."
        ) from exc

    if profile is None:
        raise HTTPException(status_code=404, detail="Ticker non trovato.")

    symbol = Symbol(
        ticker=ticker,
        name=str(_profile_get(profile, "name", "") or ""),
        exchange=_profile_get(profile, "exchange", None),
        currency=str(_profile_get(profile, "currency", "USD") or "USD"),
        asset_type=str(_profile_get(profile, "asset_type", "EQUITY") or "EQUITY"),
    )
    db.add(symbol)
    db.commit()
    db.refresh(symbol)

    try:
        await asyncio.to_thread(market_service.refresh_prices, db, symbol)
    except Exception:
        # The symbol is created either way; missing history is refreshed lazily
        # on the first GET .../prices call (see app.api.prices).
        logger.warning("Seed storico fallito per %s", ticker, exc_info=True)

    return SymbolOut.model_validate(symbol)


@router.delete(
    "/symbols/{symbol_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    response_class=Response,
)
def delete_symbol(symbol_id: int, db: Session = Depends(get_db)) -> None:
    """Delete a symbol; cascades to its prices, runs and recommendations."""
    symbol = db.get(Symbol, symbol_id)
    if symbol is None:
        raise HTTPException(status_code=404, detail="Simbolo non trovato.")
    db.delete(symbol)
    db.commit()
    return None


@router.post("/symbols/{symbol_id}/favorite", response_model=SymbolOut)
def toggle_favorite(
    symbol_id: int, payload: FavoriteToggle, db: Session = Depends(get_db)
) -> SymbolOut:
    """Set or clear favorite status, stamping/clearing ``favorite_added_at``."""
    symbol = db.get(Symbol, symbol_id)
    if symbol is None:
        raise HTTPException(status_code=404, detail="Simbolo non trovato.")
    symbol.is_favorite = payload.is_favorite
    symbol.favorite_added_at = datetime.utcnow() if payload.is_favorite else None
    db.commit()
    db.refresh(symbol)
    return SymbolOut.model_validate(symbol)


async def _refresh_1d_if_stale(db: Session, symbol: Symbol) -> None:
    """Refresh the symbol's 1d prices if its latest stored bar is stale.

    Mirrors the staleness rule/pattern of ``app.api.prices._refresh_if_stale``:
    any provider/network failure is swallowed so the overview still serves
    whatever history is already stored.
    """
    last_ts = db.execute(
        select(PriceHistory.ts)
        .where(PriceHistory.symbol_id == symbol.id, PriceHistory.interval == "1d")
        .order_by(PriceHistory.ts.desc())
        .limit(1)
    ).scalar_one_or_none()

    is_stale = last_ts is None or (datetime.utcnow() - last_ts) > _STALE_THRESHOLD_1D
    if not is_stale:
        return

    market_service = MarketDataService()
    try:
        await asyncio.to_thread(market_service.refresh_prices, db, symbol, "1d", 730)
    except Exception:
        logger.warning("Refresh prezzi fallito per %s (1d)", symbol.ticker, exc_info=True)


async def _fetch_fundamentals_cached(ticker: str) -> dict:
    """Fundamentals snapshot for ``ticker`` behind a 15-minute in-memory cache.

    Never raises: any provider/network failure yields an empty dict, so the
    overview simply exposes all-None fundamentals.
    """
    now = datetime.utcnow()
    cached = _fundamentals_cache.get(ticker)
    if cached is not None and (now - cached[1]) < _FUNDAMENTALS_CACHE_TTL:
        return cached[0]

    market_service = MarketDataService()
    try:
        data = await asyncio.to_thread(market_service.get_fundamentals, ticker)
    except Exception:
        logger.warning("Fondamentali non disponibili per %s", ticker, exc_info=True)
        data = {}
    if not isinstance(data, dict):
        data = {}
    _fundamentals_cache[ticker] = (data, now)
    return data


@router.get("/symbols/{symbol_id}/overview", response_model=SymbolOverviewOut)
async def get_symbol_overview(
    symbol_id: int, db: Session = Depends(get_db)
) -> SymbolOverviewOut:
    """Full quote + fundamentals snapshot for the symbol detail page.

    Combines the latest stored 1d OHLCV bars (refreshed first if stale) with a
    cached fundamentals pull. Every derived price field is None-safe when the
    history is short or empty, and fundamentals failures never raise.
    """
    symbol = db.get(Symbol, symbol_id)
    if symbol is None:
        raise HTTPException(status_code=404, detail="Simbolo non trovato.")

    await _refresh_1d_if_stale(db, symbol)

    rows = (
        db.execute(
            select(PriceHistory)
            .where(
                PriceHistory.symbol_id == symbol.id,
                PriceHistory.interval == "1d",
                PriceHistory.ts >= datetime.utcnow() - timedelta(days=365),
            )
            .order_by(PriceHistory.ts.asc())
        )
        .scalars()
        .all()
    )

    last_price: float | None = None
    open_: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    volume: float | None = None
    updated_at: datetime | None = None
    prev_close: float | None = None
    change_1d_abs: float | None = None
    change_pct_1d: float | None = None
    avg_volume_30d: float | None = None
    week52_high: float | None = None
    week52_low: float | None = None

    if rows:
        last = rows[-1]
        last_price = last.close
        open_ = last.open
        day_high = last.high
        day_low = last.low
        volume = last.volume
        updated_at = last.ts

        if len(rows) >= 2:
            prev_close = rows[-2].close
            if prev_close:
                change_1d_abs = last.close - prev_close
                change_pct_1d = (last.close - prev_close) / prev_close * 100.0

        recent_volumes = [r.volume for r in rows[-30:] if r.volume is not None]
        if recent_volumes:
            avg_volume_30d = sum(recent_volumes) / len(recent_volumes)

        highs = [r.high for r in rows if r.high is not None]
        lows = [r.low for r in rows if r.low is not None]
        if highs:
            week52_high = max(highs)
        if lows:
            week52_low = min(lows)

    fundamentals = await _fetch_fundamentals_cached(symbol.ticker)

    def _num(key: str) -> float | None:
        value = fundamentals.get(key)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def _str(key: str) -> str | None:
        value = fundamentals.get(key)
        return value if isinstance(value, str) and value.strip() else None

    return SymbolOverviewOut(
        ticker=symbol.ticker,
        name=symbol.name,
        exchange=symbol.exchange,
        currency=symbol.currency,
        asset_type=symbol.asset_type,
        last_price=last_price,
        change_1d_abs=change_1d_abs,
        change_pct_1d=change_pct_1d,
        open=open_,
        day_high=day_high,
        day_low=day_low,
        prev_close=prev_close,
        volume=volume,
        avg_volume_30d=avg_volume_30d,
        week52_high=week52_high,
        week52_low=week52_low,
        market_cap=_num("market_cap"),
        pe=_num("pe"),
        forward_pe=_num("forward_pe"),
        eps=_num("eps"),
        dividend_yield=_num("dividend_yield"),
        beta=_num("beta"),
        analyst_target=_num("analyst_target"),
        sector=_str("sector"),
        industry=_str("industry"),
        updated_at=updated_at,
    )
