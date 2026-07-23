"""Aggregates every API router under a single ``/api`` prefix.

``app.main`` only needs ``from app.api import api_router`` and
``app.include_router(api_router)`` — the individual route modules stay
self-contained and independently testable (see ``backend/tests/conftest.py``,
which mounts this same ``api_router`` onto a bare ``FastAPI()`` instance
without the production lifespan).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import (
    analysis,
    dashboard,
    evaluations,
    feedback,
    llm,
    prices,
    recommendations,
    settings,
    symbols,
    transactions,
    universe,
)

api_router = APIRouter(prefix="/api")

api_router.include_router(symbols.router)
api_router.include_router(prices.router)
api_router.include_router(analysis.router)
api_router.include_router(recommendations.router)
api_router.include_router(transactions.router)
api_router.include_router(evaluations.router)
api_router.include_router(feedback.router)
api_router.include_router(settings.router)
api_router.include_router(dashboard.router)
api_router.include_router(universe.router)
api_router.include_router(llm.router)

__all__ = ["api_router"]
