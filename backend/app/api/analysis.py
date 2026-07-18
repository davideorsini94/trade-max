"""Analysis run endpoints (blueprint section 4, rows 7-8).

Triggering an analysis creates a ``PENDING`` ``AnalysisRun`` row synchronously
(so the client immediately gets a ``run_id`` to poll) and hands the actual
multi-agent pipeline off to ``app.engine.orchestrator.run_analysis`` as a
detached ``asyncio`` task. ``GET /api/runs/{run_id}`` is the polling endpoint
the frontend hits every few seconds while a run is in flight.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.recommendations import reco_to_out
from app.engine import orchestrator
from app.models import Analysis, AnalysisRun, Recommendation, Symbol
from app.schemas import AnalysisOut, AnalysisRunOut, AnalyzeAccepted, AnalyzeRequest, RunStatus, Stance

logger = logging.getLogger(__name__)

router = APIRouter(tags=["analysis"])

# Keep references to fire-and-forget tasks so they are not garbage-collected
# mid-flight (a well-known asyncio.create_task pitfall).
_background_tasks: set[asyncio.Task] = set()


def analysis_to_out(a: Analysis) -> AnalysisOut:
    """Serialize an ``Analysis`` ORM row, parsing ``output_json`` into a dict."""
    try:
        output = json.loads(a.output_json) if a.output_json else {}
    except (json.JSONDecodeError, TypeError):
        logger.warning("output_json malformato per analysis %s", a.id)
        output = {}
    return AnalysisOut(
        id=a.id,
        agent_name=a.agent_name,
        status=a.status,
        signal=a.signal,
        confidence=a.confidence,
        stance=Stance(a.stance) if a.stance else None,
        summary_it=a.summary_it or "",
        output=output if isinstance(output, dict) else {},
        created_at=a.created_at,
    )


def run_to_out(
    run: AnalysisRun,
    ticker: str,
    analyses: list[Analysis],
    reco: Recommendation | None,
) -> AnalysisRunOut:
    """Assemble the full ``AnalysisRunOut`` (run + its analyses + recommendation)."""
    return AnalysisRunOut(
        id=run.id,
        symbol_id=run.symbol_id,
        ticker=ticker,
        status=RunStatus(run.status),
        trigger=run.trigger,
        llm_provider_used=run.llm_provider_used,
        error=run.error,
        started_at=run.started_at,
        finished_at=run.finished_at,
        analyses=[analysis_to_out(a) for a in analyses],
        recommendation=reco_to_out(reco, ticker) if reco is not None else None,
    )


@router.post(
    "/symbols/{symbol_id}/analyze",
    response_model=AnalyzeAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_analysis(
    symbol_id: int, payload: AnalyzeRequest, db: Session = Depends(get_db)
) -> AnalyzeAccepted:
    """Create a PENDING run and schedule the multi-agent pipeline in the background."""
    symbol = db.get(Symbol, symbol_id)
    if symbol is None:
        raise HTTPException(status_code=404, detail="Simbolo non trovato.")

    # No await points between this check and the commit below, so the event
    # loop cannot interleave a concurrent request here: the DB check closes
    # the window in which is_running() is still False because the background
    # task has not acquired its per-symbol lock yet (TOCTOU).
    # The started_at cutoff keeps a run left PENDING/RUNNING by a process
    # kill from blocking new analyses forever.
    pending_run = db.execute(
        select(AnalysisRun.id)
        .where(
            AnalysisRun.symbol_id == symbol_id,
            AnalysisRun.status.in_([RunStatus.PENDING.value, RunStatus.RUNNING.value]),
            AnalysisRun.started_at >= datetime.utcnow() - timedelta(hours=2),
        )
        .limit(1)
    ).first()
    if pending_run is not None or orchestrator.is_running(symbol_id):
        raise HTTPException(
            status_code=409, detail="Un'analisi è già in corso per questo simbolo."
        )

    run = AnalysisRun(symbol_id=symbol_id, status=RunStatus.PENDING.value, trigger=payload.trigger)
    db.add(run)
    db.commit()
    db.refresh(run)

    task = asyncio.create_task(
        orchestrator.run_analysis(symbol_id=symbol_id, trigger=payload.trigger, run_id=run.id)
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return AnalyzeAccepted(
        run_id=run.id,
        status=RunStatus.PENDING,
        message_it="Analisi avviata: elaborazione in corso in background.",
    )


def _serialize_run(run: AnalysisRun, db: Session) -> AnalysisRunOut:
    """Load a run's ticker, analyses and recommendation, then build its schema.

    Shared by ``GET /runs/{run_id}`` and ``GET /symbols/{symbol_id}/runs/latest``
    so both endpoints serialize a run identically.
    """
    symbol = db.get(Symbol, run.symbol_id)
    ticker = symbol.ticker if symbol is not None else ""

    analyses = (
        db.execute(
            select(Analysis).where(Analysis.run_id == run.id).order_by(Analysis.created_at.asc())
        )
        .scalars()
        .all()
    )
    reco = db.execute(
        select(Recommendation).where(Recommendation.run_id == run.id)
    ).scalar_one_or_none()

    return run_to_out(run, ticker, analyses, reco)


@router.get("/runs/{run_id}", response_model=AnalysisRunOut)
def get_run(run_id: int, db: Session = Depends(get_db)) -> AnalysisRunOut:
    """Polling endpoint: current status of a run plus per-agent analyses so far."""
    run = db.get(AnalysisRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run di analisi non trovata.")

    return _serialize_run(run, db)


@router.get("/symbols/{symbol_id}/runs/latest", response_model=AnalysisRunOut)
def get_latest_run(symbol_id: int, db: Session = Depends(get_db)) -> AnalysisRunOut:
    """Most recent run for a symbol (by ``started_at``, id desc as tiebreak).

    Lets the frontend detect and resume an in-flight analysis after navigating
    away and back, since ``run_id`` is otherwise only known to the tab that
    triggered the run.
    """
    symbol = db.get(Symbol, symbol_id)
    if symbol is None:
        raise HTTPException(status_code=404, detail="Simbolo non trovato.")

    run = db.execute(
        select(AnalysisRun)
        .where(AnalysisRun.symbol_id == symbol_id)
        .order_by(AnalysisRun.started_at.desc(), AnalysisRun.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Nessuna analisi per questo simbolo.")

    return _serialize_run(run, db)
