"""Agent feedback / lessons-learned endpoint (blueprint section 4, row 15)."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import AgentFeedback
from app.schemas import AgentFeedbackOut

logger = logging.getLogger(__name__)

router = APIRouter(tags=["feedback"])


def feedback_to_out(f: AgentFeedback) -> AgentFeedbackOut:
    """Serialize an ``AgentFeedback`` ORM row, parsing ``lessons_json`` into a list."""
    try:
        lessons = json.loads(f.lessons_json) if f.lessons_json else []
    except (json.JSONDecodeError, TypeError):
        logger.warning("lessons_json malformato per agent_feedback %s", f.id)
        lessons = []
    if not isinstance(lessons, list):
        lessons = []
    return AgentFeedbackOut(
        id=f.id,
        evaluation_id=f.evaluation_id,
        agent_name=f.agent_name,
        accuracy=f.accuracy,
        lessons=[str(item) for item in lessons],
        lessons_it=f.lessons_it or "",
        is_active=f.is_active,
        created_at=f.created_at,
    )


@router.get("/feedback", response_model=list[AgentFeedbackOut])
def list_feedback(
    agent_name: str | None = Query(None),
    active_only: bool = Query(True),
    db: Session = Depends(get_db),
) -> list[AgentFeedbackOut]:
    """List agent feedback/lessons, optionally filtered by agent and active status."""
    stmt = select(AgentFeedback)
    if agent_name:
        stmt = stmt.where(AgentFeedback.agent_name == agent_name)
    if active_only:
        stmt = stmt.where(AgentFeedback.is_active.is_(True))
    stmt = stmt.order_by(AgentFeedback.created_at.desc())
    rows = db.execute(stmt).scalars().all()
    return [feedback_to_out(f) for f in rows]
