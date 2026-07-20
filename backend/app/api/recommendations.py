"""Recommendation read endpoints (blueprint section 4, rows 9-11).

Also exposes ``reco_to_out``, the ``Recommendation`` ORM -> ``RecommendationOut``
serializer reused by ``app.api.analysis`` (embedding a recommendation inside an
``AnalysisRunOut``) and ``app.api.dashboard``.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import Recommendation, Symbol
from app.schemas import (
    Action,
    PolicyCheck,
    RecommendationListOut,
    RecommendationOut,
    Sizing,
    Verdict,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["recommendations"])


def reco_to_out(reco: Recommendation, ticker: str) -> RecommendationOut:
    """Serialize a ``Recommendation`` ORM row into its response schema.

    Parses ``policy_checks_json`` (a JSON-encoded ``list[PolicyCheck]``) into
    structured objects; malformed/missing JSON degrades to an empty list
    rather than raising, since this is read-path serialization of data that
    was already validated at write time by the orchestrator/policy engine.
    """
    try:
        raw_checks = json.loads(reco.policy_checks_json) if reco.policy_checks_json else []
    except (json.JSONDecodeError, TypeError):
        logger.warning("policy_checks_json malformato per recommendation %s", reco.id)
        raw_checks = []
    policy_checks = [PolicyCheck(**pc) for pc in raw_checks if isinstance(pc, dict)]

    # The two audience-specific notes live in the persisted synthesizer output;
    # parse defensively and expose empty/missing values as None (older rows).
    try:
        synth = json.loads(reco.synthesizer_json) if reco.synthesizer_json else {}
    except (json.JSONDecodeError, TypeError):
        logger.warning("synthesizer_json malformato per recommendation %s", reco.id)
        synth = {}
    if not isinstance(synth, dict):
        synth = {}

    def _advice(key: str) -> str | None:
        value = synth.get(key)
        return value.strip() if isinstance(value, str) and value.strip() else None

    return RecommendationOut(
        id=reco.id,
        run_id=reco.run_id,
        symbol_id=reco.symbol_id,
        ticker=ticker,
        action=Action(reco.action),
        sizing_strategy=Sizing(reco.sizing_strategy),
        confidence=reco.confidence,
        allocation_pct=reco.allocation_pct,
        allocation_amount=reco.allocation_amount,
        dca_tranches=reco.dca_tranches,
        entry_price=reco.entry_price,
        stop_loss_price=reco.stop_loss_price,
        take_profit_price=reco.take_profit_price,
        horizon_days=reco.horizon_days,
        estimated_profit_pct=reco.estimated_profit_pct,
        estimated_profit_amount=reco.estimated_profit_amount,
        rationale_it=reco.rationale_it or "",
        advice_new_investor_it=_advice("advice_new_investor_it"),
        advice_holder_it=_advice("advice_holder_it"),
        validator_verdict=Verdict(reco.validator_verdict),
        validator_notes_it=reco.validator_notes_it or "",
        policy_checks=policy_checks,
        policy_overridden=reco.policy_overridden,
        original_action=Action(reco.original_action) if reco.original_action else None,
        original_sizing=Sizing(reco.original_sizing) if reco.original_sizing else None,
        evaluated=reco.evaluated,
        realized_return_7d=reco.realized_return_7d,
        outcome_score=reco.outcome_score,
        created_at=reco.created_at,
    )


@router.get("/symbols/{symbol_id}/recommendations/latest", response_model=RecommendationOut)
def latest_recommendation_for_symbol(
    symbol_id: int, db: Session = Depends(get_db)
) -> RecommendationOut:
    """Return the newest recommendation for a single symbol."""
    symbol = db.get(Symbol, symbol_id)
    if symbol is None:
        raise HTTPException(status_code=404, detail="Simbolo non trovato.")

    reco = db.execute(
        select(Recommendation)
        .where(Recommendation.symbol_id == symbol_id)
        .order_by(Recommendation.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if reco is None:
        raise HTTPException(
            status_code=404, detail="Nessuna raccomandazione disponibile per questo simbolo."
        )
    return reco_to_out(reco, symbol.ticker)


@router.get("/symbols/{symbol_id}/recommendations", response_model=RecommendationListOut)
def list_recommendations_for_symbol(
    symbol_id: int,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> RecommendationListOut:
    """Return a page of recommendations for a symbol, newest first."""
    symbol = db.get(Symbol, symbol_id)
    if symbol is None:
        raise HTTPException(status_code=404, detail="Simbolo non trovato.")

    total = db.execute(
        select(func.count()).select_from(Recommendation).where(Recommendation.symbol_id == symbol_id)
    ).scalar_one()
    rows = (
        db.execute(
            select(Recommendation)
            .where(Recommendation.symbol_id == symbol_id)
            .order_by(Recommendation.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return RecommendationListOut(
        items=[reco_to_out(r, symbol.ticker) for r in rows], total=total
    )


@router.get("/recommendations/latest", response_model=list[RecommendationOut])
def latest_recommendations(db: Session = Depends(get_db)) -> list[RecommendationOut]:
    """Return the newest recommendation for every active symbol that has one."""
    active_symbols = db.execute(select(Symbol).where(Symbol.is_active.is_(True))).scalars().all()

    results: list[RecommendationOut] = []
    for symbol in active_symbols:
        reco = db.execute(
            select(Recommendation)
            .where(Recommendation.symbol_id == symbol.id)
            .order_by(Recommendation.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if reco is not None:
            results.append(reco_to_out(reco, symbol.ticker))
    return results
