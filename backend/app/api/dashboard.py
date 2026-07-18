"""Dashboard summary + health endpoints (blueprint section 4, rows 18-19).

``GET /api/dashboard/summary`` is the single polling endpoint the frontend
dashboard hits every 30s: it assembles favorites/others with stored (not
live) quotes, the last evaluation, pending-run count, current settings and
market-open status, plus the disclaimer text (blueprint section 8) that must
always be present in the payload.

``GET /api/health`` reports DB connectivity, scheduler status and which LLM
providers are configured — never their actual key values.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.evaluations import evaluation_to_out
from app.api.settings import get_or_create_settings
from app.api.symbols import build_symbol_with_quote
from app.config import get_settings
from app.models import AnalysisRun, Evaluation, Symbol
from app.schemas import DashboardSummary, HealthOut, ProviderStatus, SettingsOut

logger = logging.getLogger(__name__)

router = APIRouter(tags=["dashboard"])

# Exact disclaimer text from blueprint section 8 — must always be present.
DISCLAIMER_IT = (
    "⚠️ TradeMax è uno strumento sperimentale a scopo informativo. "
    "Non costituisce consulenza finanziaria. Le decisioni di investimento sono "
    "a tuo esclusivo rischio."
)


def _is_scheduler_running() -> bool:
    """Best-effort check of whether the APScheduler instance is active.

    Defensive against the exact module-level attribute name used in
    ``app.scheduler`` (owned by another module) — tries the conventional
    names in order and fails closed (``False``) if none are found or the
    module cannot be imported (e.g. under test fixtures with no scheduler).
    """
    try:
        from app import scheduler as scheduler_module
    except Exception:
        return False
    for attr_name in ("scheduler", "_scheduler", "async_scheduler"):
        sched = getattr(scheduler_module, attr_name, None)
        if sched is not None:
            return bool(getattr(sched, "running", False))
    return False


def _is_market_open() -> bool:
    """Best-effort call into ``app.scheduler.is_market_open``, failing closed."""
    try:
        from app.scheduler import is_market_open
    except Exception:
        return False
    try:
        return bool(is_market_open())
    except Exception:
        logger.warning("is_market_open() ha sollevato un'eccezione", exc_info=True)
        return False


@router.get("/dashboard/summary", response_model=DashboardSummary)
def dashboard_summary(db: Session = Depends(get_db)) -> DashboardSummary:
    """Assemble the single-endpoint dashboard payload the frontend polls."""
    favorite_rows = (
        db.execute(
            select(Symbol)
            .where(Symbol.is_active.is_(True), Symbol.is_favorite.is_(True))
            .order_by(Symbol.favorite_added_at.asc())
        )
        .scalars()
        .all()
    )
    other_rows = (
        db.execute(
            select(Symbol)
            .where(Symbol.is_active.is_(True), Symbol.is_favorite.is_(False))
            .order_by(Symbol.ticker.asc())
        )
        .scalars()
        .all()
    )

    favorites = [build_symbol_with_quote(db, s, with_quotes=True) for s in favorite_rows]
    others = [build_symbol_with_quote(db, s, with_quotes=True) for s in other_rows]

    last_eval_row = db.execute(
        select(Evaluation).order_by(Evaluation.created_at.desc()).limit(1)
    ).scalar_one_or_none()
    last_evaluation = evaluation_to_out(db, last_eval_row) if last_eval_row is not None else None

    pending_runs = db.execute(
        select(func.count())
        .select_from(AnalysisRun)
        .where(AnalysisRun.status.in_(["PENDING", "RUNNING"]))
    ).scalar_one()

    settings_row = get_or_create_settings(db)
    budget = SettingsOut.model_validate(settings_row)

    return DashboardSummary(
        favorites=favorites,
        others=others,
        last_evaluation=last_evaluation,
        pending_runs=pending_runs,
        budget=budget,
        market_open=_is_market_open(),
        disclaimer_it=DISCLAIMER_IT,
    )


@router.get("/health", response_model=HealthOut)
def health(db: Session = Depends(get_db)) -> HealthOut:
    """Report DB connectivity, scheduler status and LLM provider configuration."""
    try:
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        logger.error("Health check DB fallito", exc_info=True)
        db_ok = False

    settings = get_settings()
    primary = settings.llm_provider
    providers = [
        ProviderStatus(
            provider="openrouter",
            configured=settings.openrouter_configured,
            model=settings.openrouter_model,
            is_primary=(primary == "openrouter"),
        ),
        ProviderStatus(
            provider="gemini",
            configured=settings.gemini_configured,
            model=settings.gemini_model,
            is_primary=(primary == "gemini"),
        ),
    ]

    return HealthOut(
        status="ok" if db_ok else "degraded",
        db_ok=db_ok,
        scheduler_running=_is_scheduler_running(),
        providers=providers,
    )
