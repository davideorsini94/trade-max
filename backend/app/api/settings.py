"""Application settings endpoints (blueprint section 4, rows 16-17).

``app_settings`` is a single-row table (``id=1``), normally seeded at
bootstrap by ``app.main``'s lifespan. The get-or-create helper here makes the
router resilient even if that seed hasn't run yet (e.g. under the test
fixtures in ``backend/tests/conftest.py``, which build the app without the
real lifespan).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import AppSettings
from app.schemas import RiskProfile, SettingsOut, SettingsUpdate

router = APIRouter(tags=["settings"])


def get_or_create_settings(db: Session) -> AppSettings:
    """Return the singleton settings row, creating it with defaults if absent.

    Public (no leading underscore) because ``app.api.dashboard`` reuses it to
    embed the current budget/risk settings in the dashboard summary.
    """
    row = db.get(AppSettings, 1)
    if row is None:
        row = AppSettings(id=1)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


@router.get("/settings", response_model=SettingsOut)
def get_settings_endpoint(db: Session = Depends(get_db)) -> SettingsOut:
    """Return the current application settings."""
    row = get_or_create_settings(db)
    return SettingsOut.model_validate(row)


@router.put("/settings", response_model=SettingsOut)
def update_settings_endpoint(payload: SettingsUpdate, db: Session = Depends(get_db)) -> SettingsOut:
    """Partially update application settings; only fields set on the payload change."""
    row = get_or_create_settings(db)
    updates = payload.model_dump(exclude_unset=True, exclude_none=True)
    for field, value in updates.items():
        if isinstance(value, RiskProfile):
            value = value.value
        setattr(row, field, value)
    db.commit()
    db.refresh(row)
    return SettingsOut.model_validate(row)
