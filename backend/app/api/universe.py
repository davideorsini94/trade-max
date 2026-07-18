"""Curated stock universe ("Universo titoli") endpoints — Mercato page backend.

Exposes a paginated, sortable view over ``universe_stats`` (rank by composite /
fame / trend / value / reliability) enriched with the caller's monitoring state
(each row knows whether the ticker is already a monitored ``Symbol`` and/or a
favorite), plus a manual refresh trigger.

Adding a universe entry to monitoring/favorites reuses the existing
``POST /api/symbols`` and ``POST /api/symbols/{id}/favorite`` endpoints — this
module only *reflects* that state, it never duplicates the write paths.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.data import universe
from app.db import session_scope
from app.models import Symbol, UniverseStat
from app.schemas import UniverseItemOut, UniversePageOut

logger = logging.getLogger(__name__)

router = APIRouter(tags=["universe"])

# page_size clamp bounds (blueprint spec).
_PAGE_SIZE_MIN = 5
_PAGE_SIZE_MAX = 100

# sort key -> orderable column. "fame" uses composite_score as a tiebreak.
_SORT_COLUMNS = {
    "composite": UniverseStat.composite_score,
    "fame": UniverseStat.fame_rank,
    "trend": UniverseStat.change_pct_30d,
    "value": UniverseStat.market_cap_bn,
    "reliability": UniverseStat.reliability_score,
}

# Keeps a reference to the most recent background refresh task so it is not
# garbage-collected while pending (asyncio only holds a weak reference).
_background_task: asyncio.Task | None = None


def _refresh_with_session() -> int:
    """Run a full universe refresh in its own session (called via to_thread)."""
    with session_scope() as db:
        return universe.refresh_universe(db)


def _kick_background_refresh() -> None:
    """Schedule a background universe refresh, guarded by the refresh lock.

    No-op if a refresh is already running or if there is no running event loop
    (e.g. when called from a sync/threadpool context). Failures never propagate
    to the caller.
    """
    global _background_task
    if universe.is_refreshing():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("No running event loop; skipping background universe refresh")
        return
    _background_task = loop.create_task(asyncio.to_thread(_refresh_with_session))


def _nulls_last(column, descending: bool) -> list:
    """Order-by clauses sorting ``column`` with NULLs ALWAYS last.

    Portable across SQLite versions (uses a ``CASE`` null-flag as the primary
    key rather than the native ``NULLS LAST`` syntax).
    """
    null_flag = case((column.is_(None), 1), else_=0).asc()
    direction = column.desc() if descending else column.asc()
    return [null_flag, direction]


@router.get("/universe", response_model=UniversePageOut)
async def get_universe(
    sort: str = Query("composite"),
    order: str = Query("desc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25),
    q: str | None = Query(None),
    db: Session = Depends(get_db),
) -> UniversePageOut:
    """Paginated, sortable curated universe with per-row monitoring state."""
    # Bootstrap: seed static metadata synchronously (no network) the first time
    # the table is hit, then kick a background refresh to fill in market data.
    total_rows = db.execute(select(func.count()).select_from(UniverseStat)).scalar_one()
    if total_rows == 0:
        try:
            universe.seed_universe(db)
        except Exception:
            logger.warning("Seed iniziale dell'universo fallito", exc_info=True)
        _kick_background_refresh()

    page_size = max(_PAGE_SIZE_MIN, min(_PAGE_SIZE_MAX, page_size))
    descending = order != "asc"

    base = select(UniverseStat)
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        base = base.where(
            or_(UniverseStat.ticker.ilike(pattern), UniverseStat.name.ilike(pattern))
        )

    total = db.execute(
        select(func.count()).select_from(base.subquery())
    ).scalar_one()

    sort_column = _SORT_COLUMNS.get(sort, UniverseStat.composite_score)
    order_clauses = _nulls_last(sort_column, descending)
    if sort == "fame":
        # Tiebreak most-famous-first rows by composite (always desc).
        order_clauses += _nulls_last(UniverseStat.composite_score, True)
    # Deterministic final tiebreak so pagination is stable.
    order_clauses.append(UniverseStat.ticker.asc())

    offset = (page - 1) * page_size
    rows = (
        db.execute(base.order_by(*order_clauses).offset(offset).limit(page_size))
        .scalars()
        .all()
    )

    # Single query mapping ticker -> monitoring state.
    symbol_map: dict[str, tuple[int, bool]] = {}
    tickers = [row.ticker for row in rows]
    if tickers:
        for sym_id, sym_ticker, sym_fav in db.execute(
            select(Symbol.id, Symbol.ticker, Symbol.is_favorite).where(
                Symbol.ticker.in_(tickers)
            )
        ).all():
            symbol_map[sym_ticker.upper()] = (sym_id, bool(sym_fav))

    items: list[UniverseItemOut] = []
    for row in rows:
        sym = symbol_map.get(row.ticker.upper())
        item = UniverseItemOut.model_validate(row)
        item.monitored = sym is not None
        item.is_favorite = sym[1] if sym is not None else False
        item.symbol_id = sym[0] if sym is not None else None
        items.append(item)

    last_refresh = db.execute(select(func.max(UniverseStat.updated_at))).scalar_one()

    return UniversePageOut(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        refreshing=universe.is_refreshing(),
        last_refresh=last_refresh,
    )


@router.post("/universe/refresh", status_code=status.HTTP_202_ACCEPTED)
async def refresh_universe_endpoint() -> dict[str, str]:
    """Kick a background refresh of the whole universe's market stats."""
    if universe.is_refreshing():
        raise HTTPException(status_code=409, detail="Aggiornamento già in corso.")
    _kick_background_refresh()
    return {"detail": "Aggiornamento avviato."}
