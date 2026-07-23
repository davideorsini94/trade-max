"""Weekly evaluation endpoints (blueprint section 4, rows 12-14).

Also exposes ``evaluation_to_out``, reused by ``app.api.dashboard`` to embed
the last evaluation in the dashboard summary.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.evaluation.evaluator import get_pending_status, run_weekly_evaluation
from app.models import AgentFeedback, Evaluation
from app.schemas import AgentMetrics, EvaluationOut, PendingEvaluationOut

logger = logging.getLogger(__name__)

router = APIRouter(tags=["evaluations"])

# Guards against overlapping manual triggers of the (potentially slow, LLM-backed)
# weekly evaluation job. The scheduled cron job itself may have its own
# concurrency guard internally; this one specifically protects the manual
# endpoint from being hit twice in quick succession.
_evaluation_lock = asyncio.Lock()


def _accuracy_trend(db: Session, agent_name: str, up_to: datetime, limit: int = 8) -> list[float]:
    """Last ``limit`` known accuracy values for ``agent_name`` up to ``up_to``, chronological.

    Bounded by the *evaluation's* timestamp (via the feedback→evaluation join),
    not the feedback row's own ``created_at``: feedback rows are written a few
    seconds after their Evaluation, so filtering on their own timestamp would
    always exclude the current evaluation's accuracy (off-by-one lag).
    """
    rows = (
        db.execute(
            select(AgentFeedback.accuracy)
            .join(Evaluation, AgentFeedback.evaluation_id == Evaluation.id)
            .where(AgentFeedback.agent_name == agent_name, Evaluation.created_at <= up_to)
            .order_by(Evaluation.created_at.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    values = [v for v in rows if v is not None]
    return list(reversed(values))


def evaluation_to_out(db: Session, ev: Evaluation) -> EvaluationOut:
    """Serialize an ``Evaluation`` ORM row, parsing ``per_agent_json`` into ``AgentMetrics``."""
    try:
        per_agent_raw = json.loads(ev.per_agent_json) if ev.per_agent_json else {}
    except (json.JSONDecodeError, TypeError):
        logger.warning("per_agent_json malformato per evaluation %s", ev.id)
        per_agent_raw = {}
    if not isinstance(per_agent_raw, dict):
        per_agent_raw = {}

    try:
        feature_stats = json.loads(ev.feature_stats_json) if ev.feature_stats_json else None
    except (json.JSONDecodeError, TypeError):
        logger.warning("feature_stats_json malformato per evaluation %s", ev.id)
        feature_stats = None
    if not isinstance(feature_stats, dict):
        feature_stats = None

    per_agent: list[AgentMetrics] = []
    for agent_name, metrics in per_agent_raw.items():
        metrics = metrics if isinstance(metrics, dict) else {}
        final = metrics.get("final")
        final = final if isinstance(final, dict) else {}
        per_agent.append(
            AgentMetrics(
                agent_name=agent_name,
                accuracy=metrics.get("accuracy"),
                avg_signal_error=metrics.get("avg_signal_error"),
                n_samples=int(metrics.get("n_samples") or 0),
                trend=_accuracy_trend(db, agent_name, ev.created_at),
                accuracy_final=final.get("accuracy"),
                n_samples_final=int(final.get("n_samples") or 0),
            )
        )

    return EvaluationOut(
        id=ev.id,
        period_start=ev.period_start,
        period_end=ev.period_end,
        status=ev.status,
        total_recommendations=ev.total_recommendations,
        evaluated_count=ev.evaluated_count,
        accuracy_overall=ev.accuracy_overall,
        avg_realized_return_pct=ev.avg_realized_return_pct,
        hypothetical_pnl_pct=ev.hypothetical_pnl_pct,
        best_symbol=ev.best_symbol,
        worst_symbol=ev.worst_symbol,
        per_agent=per_agent,
        report_it=ev.report_it or "",
        created_at=ev.created_at,
        feature_stats=feature_stats,
    )


@router.get("/evaluations", response_model=list[EvaluationOut])
def list_evaluations(
    limit: int = Query(12, ge=1, le=100), db: Session = Depends(get_db)
) -> list[EvaluationOut]:
    """List the most recent weekly evaluations, newest first."""
    rows = (
        db.execute(select(Evaluation).order_by(Evaluation.created_at.desc()).limit(limit))
        .scalars()
        .all()
    )
    return [evaluation_to_out(db, ev) for ev in rows]


@router.get("/evaluations/pending", response_model=PendingEvaluationOut)
def get_pending_evaluations(db: Session = Depends(get_db)) -> PendingEvaluationOut:
    """Not-yet-scoreable recommendations (need 7 days of history to be scored)."""
    return PendingEvaluationOut(**get_pending_status(db))


@router.get("/evaluations/{evaluation_id}", response_model=EvaluationOut)
def get_evaluation(evaluation_id: int, db: Session = Depends(get_db)) -> EvaluationOut:
    """Return a single evaluation by id."""
    ev = db.get(Evaluation, evaluation_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="Valutazione non trovata.")
    return evaluation_to_out(db, ev)


@router.post("/evaluations/run", response_model=EvaluationOut, status_code=status.HTTP_201_CREATED)
async def trigger_evaluation(db: Session = Depends(get_db)) -> EvaluationOut:
    """Manually trigger the weekly evaluation job (also runs on a Sunday cron)."""
    if _evaluation_lock.locked():
        raise HTTPException(status_code=409, detail="Una valutazione è già in corso.")

    async with _evaluation_lock:
        try:
            evaluation = await run_weekly_evaluation()
        except RuntimeError as exc:
            # The evaluator's own lock is held (e.g. by the Sunday cron job).
            raise HTTPException(status_code=409, detail="Una valutazione è già in corso.") from exc
        except Exception as exc:
            logger.error("Valutazione settimanale fallita: %s", exc, exc_info=True)
            raise HTTPException(
                status_code=500, detail="Esecuzione della valutazione fallita."
            ) from exc

    if evaluation is None:
        raise HTTPException(
            status_code=500, detail="Nessuna valutazione generata (dati insufficienti)."
        )

    # Accept either the persisted Evaluation ORM instance or a bare id: the
    # evaluator may have used its own session, so we always re-fetch through
    # the request-scoped session to guarantee a fully attached, fresh instance.
    evaluation_id = evaluation.id if hasattr(evaluation, "id") else evaluation
    fresh = db.get(Evaluation, evaluation_id)
    if fresh is None:
        raise HTTPException(status_code=500, detail="Valutazione creata ma non recuperabile.")
    return evaluation_to_out(db, fresh)
