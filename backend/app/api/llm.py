"""LLM model-selection endpoints (per provider AND per actor).

Three endpoints back the model-selection screen:

* ``GET  /api/llm/models`` — the models a configured provider currently offers
  (fetched live from the provider, cached per-provider for 1h);
* ``GET  /api/llm/config`` — provider configuration state plus the stored
  ``default`` / ``per_agent`` preferences;
* ``PUT  /api/llm/config`` — replace the full desired preference state.

Preferences live in the ``llm_model_prefs`` table; the runtime resolver is
``app.llm.prefs`` (whose cache this module invalidates on write).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import Settings, get_settings
from app.llm.prefs import invalidate_prefs_cache
from app.models import LlmModelPref
from app.schemas import (
    LlmConfigOut,
    LlmConfigUpdate,
    LlmModelInfo,
    LlmModelRef,
    LlmModelsOut,
    LlmProviderInfo,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/llm", tags=["llm"])

#: Valid provider identifiers (mirrors ``LLMClient`` / ``Settings``).
_VALID_PROVIDERS: tuple[str, ...] = ("openrouter", "gemini")

#: The six actor slots plus the desk-wide ``default`` slot.
_AGENT_NAMES: tuple[str, ...] = (
    "technical",
    "fundamentals",
    "macro_news",
    "corporate_news",
    "synthesizer",
    "validator",
)

_UPSTREAM_TIMEOUT_S = 30.0

# Per-provider models cache: provider -> (monotonic_fetch_time, response).
_MODELS_CACHE: dict[str, tuple[float, LlmModelsOut]] = {}
_MODELS_TTL_S = 3600.0


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _provider_configured(settings: Settings, provider: str) -> bool:
    if provider == "openrouter":
        return settings.openrouter_configured
    if provider == "gemini":
        return settings.gemini_configured
    return False


def _provider_default_model(settings: Settings, provider: str) -> str:
    if provider == "openrouter":
        return settings.openrouter_model
    if provider == "gemini":
        return settings.gemini_model
    return ""


def _provider_api_key(settings: Settings, provider: str) -> str:
    if provider == "openrouter":
        return settings.openrouter_api_key
    if provider == "gemini":
        return settings.gemini_api_key
    return ""


# --------------------------------------------------------------------------- #
# Upstream model listings
# --------------------------------------------------------------------------- #


async def _fetch_openrouter_models(api_key: str) -> list[LlmModelInfo]:
    """Fetch OpenRouter's model catalogue (id + name), sorted by id."""
    async with httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT_S) as client:
        response = await client.get(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        response.raise_for_status()
        data = response.json()
    items = data.get("data") if isinstance(data, dict) else None
    models: list[LlmModelInfo] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        label = item.get("name") or model_id
        models.append(LlmModelInfo(id=model_id, label=str(label)))
    models.sort(key=lambda model: model.id)
    return models


async def _fetch_gemini_models(api_key: str) -> list[LlmModelInfo]:
    """Fetch Gemini models supporting generateContent (id without ``models/``)."""
    async with httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT_S) as client:
        response = await client.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": api_key},
        )
        response.raise_for_status()
        data = response.json()
    items = data.get("models") if isinstance(data, dict) else None
    models: list[LlmModelInfo] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        methods = item.get("supportedGenerationMethods") or []
        if "generateContent" not in methods:
            continue
        raw_name = item.get("name")
        if not isinstance(raw_name, str) or not raw_name:
            continue
        model_id = raw_name[len("models/") :] if raw_name.startswith("models/") else raw_name
        label = item.get("displayName") or model_id
        models.append(LlmModelInfo(id=model_id, label=str(label)))
    models.sort(key=lambda model: model.id)
    return models


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@router.get("/models", response_model=LlmModelsOut)
async def get_models(provider: str) -> LlmModelsOut:
    """Return the models ``provider`` currently offers (cached per provider, 1h TTL).

    400 when the provider is unknown or its API key is not configured; 502 when
    the upstream fetch fails.
    """
    settings = get_settings()
    if provider not in _VALID_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Provider non valido: {provider}.")
    if not _provider_configured(settings, provider):
        raise HTTPException(
            status_code=400,
            detail=f"Provider {provider} non configurato (chiave API mancante).",
        )

    now = time.monotonic()
    cached = _MODELS_CACHE.get(provider)
    if cached is not None and (now - cached[0]) < _MODELS_TTL_S:
        return cached[1]

    api_key = _provider_api_key(settings, provider)
    try:
        if provider == "openrouter":
            models = await _fetch_openrouter_models(api_key)
        else:
            models = await _fetch_gemini_models(api_key)
    except Exception as exc:
        logger.warning("Recupero modelli da %s fallito: %s", provider, exc)
        raise HTTPException(
            status_code=502,
            detail=f"Impossibile recuperare l'elenco dei modelli da {provider}.",
        ) from exc

    result = LlmModelsOut(provider=provider, models=models, fetched_at=datetime.utcnow())
    _MODELS_CACHE[provider] = (now, result)
    return result


def _build_config(db: Session, settings: Settings) -> LlmConfigOut:
    """Assemble the ``GET /api/llm/config`` payload from settings + prefs table."""
    rows = {row.agent_name: row for row in db.query(LlmModelPref).all()}
    default_row = rows.get("default")
    # is_primary reflects the EFFECTIVE default provider: DB default if set,
    # else the env-configured LLM_PROVIDER.
    effective_primary = default_row.provider if default_row is not None else settings.llm_provider

    providers = [
        LlmProviderInfo(
            provider=name,
            configured=_provider_configured(settings, name),
            is_primary=(effective_primary == name),
            env_default_model=_provider_default_model(settings, name),
        )
        for name in _VALID_PROVIDERS
    ]
    default_ref = (
        LlmModelRef(provider=default_row.provider, model=default_row.model)
        if default_row is not None
        else None
    )
    per_agent: dict[str, LlmModelRef | None] = {}
    for name in _AGENT_NAMES:
        row = rows.get(name)
        per_agent[name] = (
            LlmModelRef(provider=row.provider, model=row.model) if row is not None else None
        )
    return LlmConfigOut(providers=providers, default=default_ref, per_agent=per_agent)


@router.get("/config", response_model=LlmConfigOut)
def get_config(db: Session = Depends(get_db)) -> LlmConfigOut:
    """Return provider configuration state plus stored default/per-agent prefs."""
    return _build_config(db, get_settings())


@router.put("/config", response_model=LlmConfigOut)
def update_config(payload: LlmConfigUpdate, db: Session = Depends(get_db)) -> LlmConfigOut:
    """Replace the FULL desired LLM model-preference state.

    The body carries the complete desired state: ``default`` and all six
    ``per_agent`` entries. For each slot a present ``{provider, model}`` upserts
    the row and ``null`` (or an omitted slot) unsets it. Every non-null selection
    is validated (provider in {openrouter, gemini}, that provider configured, and
    a non-empty model) before anything is written; on success the prefs cache is
    invalidated and the fresh ``GET`` shape is returned.
    """
    settings = get_settings()

    def _validate(ref: LlmModelRef) -> None:
        if ref.provider not in _VALID_PROVIDERS:
            raise HTTPException(status_code=400, detail=f"Provider non valido: {ref.provider}.")
        if not _provider_configured(settings, ref.provider):
            raise HTTPException(
                status_code=400,
                detail=f"Provider {ref.provider} non configurato (chiave API mancante).",
            )
        if not isinstance(ref.model, str) or not ref.model.strip():
            raise HTTPException(
                status_code=400, detail="Il modello deve essere una stringa non vuota."
            )

    # Full desired state: the default slot plus every known agent slot.
    desired: dict[str, LlmModelRef | None] = {"default": payload.default}
    for name in _AGENT_NAMES:
        desired[name] = payload.per_agent.get(name)

    # Validate everything before mutating anything.
    for ref in desired.values():
        if ref is not None:
            _validate(ref)

    for name, ref in desired.items():
        existing = db.get(LlmModelPref, name)
        if ref is None:
            if existing is not None:
                db.delete(existing)
        elif existing is None:
            db.add(LlmModelPref(agent_name=name, provider=ref.provider, model=ref.model.strip()))
        else:
            existing.provider = ref.provider
            existing.model = ref.model.strip()

    db.commit()
    invalidate_prefs_cache()
    return _build_config(db, settings)
