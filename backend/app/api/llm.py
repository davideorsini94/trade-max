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
from app.llm.prefs import invalidate_prefs_cache
from app.llm.runtime import EffectiveLlmConfig, get_effective, invalidate_effective
from app.models import LlmModelPref, LlmProviderSettings
from app.schemas import (
    LlmConfigOut,
    LlmConfigUpdate,
    LlmModelInfo,
    LlmModelRef,
    LlmModelsOut,
    LlmProviderInfo,
    LlmProvidersOut,
    LlmProviderState,
    LlmProvidersUpdate,
    LlmProviderTestRequest,
    LlmProviderTestResult,
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


def _provider_configured(config: EffectiveLlmConfig, provider: str) -> bool:
    if provider == "openrouter":
        return config.openrouter_configured
    if provider == "gemini":
        return config.gemini_configured
    return False


def _provider_default_model(config: EffectiveLlmConfig, provider: str) -> str:
    if provider == "openrouter":
        return config.openrouter_model
    if provider == "gemini":
        return config.gemini_model
    return ""


def _provider_api_key(config: EffectiveLlmConfig, provider: str) -> str:
    if provider == "openrouter":
        return config.openrouter_api_key
    if provider == "gemini":
        return config.gemini_api_key
    return ""


def _mask_key(key: str) -> str:
    """Return a display-safe hint of ``key`` — never the full value.

    ``first5 + "…" + last4`` for normal keys, or ``"…" + last4`` for short keys
    (where showing the first five would reveal (nearly) the whole key).
    """
    key = key.strip()
    if len(key) < 10:
        return "…" + key[-4:]
    return key[:5] + "…" + key[-4:]


# --------------------------------------------------------------------------- #
# Upstream model listings
# --------------------------------------------------------------------------- #


async def _check_openrouter_key(api_key: str) -> None:
    """Validate an OpenRouter key against the authenticated ``auth/key`` endpoint.

    The OpenRouter models list is PUBLIC (it succeeds with any or no key), so a
    key check must hit an endpoint that actually enforces authentication.
    Raises ``httpx.HTTPStatusError`` (401/403 on a bad key) or network errors.
    """
    async with httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT_S) as client:
        response = await client.get(
            "https://openrouter.ai/api/v1/auth/key",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        response.raise_for_status()


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
    config = get_effective()
    if provider not in _VALID_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Provider non valido: {provider}.")
    if not _provider_configured(config, provider):
        raise HTTPException(
            status_code=400,
            detail=f"Provider {provider} non configurato (chiave API mancante).",
        )

    now = time.monotonic()
    cached = _MODELS_CACHE.get(provider)
    if cached is not None and (now - cached[0]) < _MODELS_TTL_S:
        return cached[1]

    api_key = _provider_api_key(config, provider)
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


def _build_config(db: Session, config: EffectiveLlmConfig) -> LlmConfigOut:
    """Assemble the ``GET /api/llm/config`` payload from effective config + prefs table."""
    rows = {row.agent_name: row for row in db.query(LlmModelPref).all()}
    default_row = rows.get("default")
    # is_primary reflects the EFFECTIVE default provider: the model-prefs default
    # row if set, otherwise the effective primary provider (DB override, else env).
    effective_primary = (
        default_row.provider if default_row is not None else config.primary_provider
    )

    providers = [
        LlmProviderInfo(
            provider=name,
            configured=_provider_configured(config, name),
            is_primary=(effective_primary == name),
            env_default_model=_provider_default_model(config, name),
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
    return _build_config(db, get_effective())


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
    config = get_effective()

    def _validate(ref: LlmModelRef) -> None:
        if ref.provider not in _VALID_PROVIDERS:
            raise HTTPException(status_code=400, detail=f"Provider non valido: {ref.provider}.")
        if not _provider_configured(config, ref.provider):
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
    return _build_config(db, config)


# --------------------------------------------------------------------------- #
# Provider settings (provider choice + API keys, managed from the Settings page)
# --------------------------------------------------------------------------- #


def _build_providers_out() -> LlmProvidersOut:
    """Assemble the ``/api/llm/providers`` payload from the effective config.

    Keys are always masked; the full value never leaves the server.
    """
    config = get_effective()
    return LlmProvidersOut(
        primary_provider=config.primary_provider,
        fallback_enabled=config.fallback_enabled,
        providers=[
            LlmProviderState(
                provider="openrouter",
                configured=config.openrouter_configured,
                source=config.openrouter_source,
                key_masked=(
                    _mask_key(config.openrouter_api_key)
                    if config.openrouter_configured
                    else None
                ),
                default_model=config.openrouter_model,
            ),
            LlmProviderState(
                provider="gemini",
                configured=config.gemini_configured,
                source=config.gemini_source,
                key_masked=(
                    _mask_key(config.gemini_api_key) if config.gemini_configured else None
                ),
                default_model=config.gemini_model,
            ),
        ],
    )


@router.get("/providers", response_model=LlmProvidersOut)
def get_providers() -> LlmProvidersOut:
    """Return the effective provider config (choice, fallback, per-provider state)."""
    return _build_providers_out()


@router.put("/providers", response_model=LlmProvidersOut)
def update_providers(
    payload: LlmProvidersUpdate, db: Session = Depends(get_db)
) -> LlmProvidersOut:
    """Persist provider-config changes to the single ``llm_provider_settings`` row.

    Only fields PRESENT in the body are touched. For the API keys: ``null``
    deletes the stored key (the ``.env`` fallback, if any, remains), a non-empty
    string is stored trimmed, and an empty string is rejected. ``primary_provider``
    (present, non-null) must be a known provider. On success the effective-config
    cache is invalidated so the change takes effect immediately, and the fresh
    ``GET`` shape is returned. Keys are never logged.
    """
    fields = payload.model_fields_set

    # Validate everything before mutating anything.
    if "primary_provider" in fields and payload.primary_provider is not None:
        if payload.primary_provider.strip().lower() not in _VALID_PROVIDERS:
            raise HTTPException(
                status_code=400, detail=f"Provider non valido: {payload.primary_provider}."
            )
    for key_field in ("openrouter_api_key", "gemini_api_key"):
        if key_field in fields:
            value = getattr(payload, key_field)
            if value is not None and not value.strip():
                raise HTTPException(status_code=400, detail="Chiave API non valida.")

    row = db.get(LlmProviderSettings, 1)
    if row is None:
        row = LlmProviderSettings(id=1)
        db.add(row)

    if "primary_provider" in fields:
        row.primary_provider = (
            payload.primary_provider.strip().lower()
            if payload.primary_provider is not None
            else None
        )
    if "fallback_enabled" in fields:
        row.fallback_enabled = payload.fallback_enabled
    if "openrouter_api_key" in fields:
        row.openrouter_api_key = (
            payload.openrouter_api_key.strip()
            if payload.openrouter_api_key is not None
            else None
        )
    if "gemini_api_key" in fields:
        row.gemini_api_key = (
            payload.gemini_api_key.strip() if payload.gemini_api_key is not None else None
        )

    db.commit()
    invalidate_effective()
    return _build_providers_out()


@router.post("/providers/test", response_model=LlmProviderTestResult)
async def test_provider(payload: LlmProviderTestRequest) -> LlmProviderTestResult:
    """Validate a provider's EFFECTIVE key by listing its models (cache bypassed).

    400 when the provider is unknown or no key is configured anywhere; otherwise
    ``ok=false`` with an Italian detail on auth/network errors, ``ok=true`` when
    the models listing succeeds.
    """
    provider = payload.provider
    if provider not in _VALID_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Provider non valido: {provider}.")

    api_key = _provider_api_key(get_effective(), provider)
    if not api_key.strip():
        raise HTTPException(
            status_code=400,
            detail=f"Provider {provider} non configurato (chiave API mancante).",
        )

    try:
        if provider == "openrouter":
            await _check_openrouter_key(api_key)
        else:
            await _fetch_gemini_models(api_key)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in (401, 403):
            return LlmProviderTestResult(
                ok=False, detail_it="Chiave API non valida o non autorizzata."
            )
        return LlmProviderTestResult(
            ok=False, detail_it=f"Il provider ha risposto con un errore (HTTP {status})."
        )
    except Exception as exc:  # network/timeout/parse errors
        logger.warning("Test del provider %s fallito: %s", provider, exc)
        return LlmProviderTestResult(
            ok=False,
            detail_it="Impossibile contattare il provider. Verifica la chiave e la connessione.",
        )

    return LlmProviderTestResult(ok=True, detail_it="Connessione riuscita: chiave valida.")
