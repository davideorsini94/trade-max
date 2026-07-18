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

import asyncio
import json
import logging
import threading
import time
from collections.abc import AsyncIterator
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
    OllamaCatalogEntry,
    OllamaLibraryInstalled,
    OllamaLibraryOut,
    OllamaPullRequest,
    OllamaPullStatusOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/llm", tags=["llm"])

#: Valid provider identifiers (mirrors ``LLMClient`` / ``Settings``).
_VALID_PROVIDERS: tuple[str, ...] = ("openrouter", "gemini", "ollama")

#: Italian detail used wherever an Ollama call is attempted with no base URL set.
_OLLAMA_NOT_CONFIGURED = "Ollama non configurato (URL di base mancante)."
#: Short timeout for the Ollama liveness probe (GET /api/version).
_OLLAMA_PROBE_TIMEOUT_S = 5.0

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
    if provider == "ollama":
        return config.ollama_configured
    return False


def _provider_default_model(config: EffectiveLlmConfig, provider: str) -> str:
    if provider == "openrouter":
        return config.openrouter_model
    if provider == "gemini":
        return config.gemini_model
    if provider == "ollama":
        return config.ollama_model
    return ""


def _provider_api_key(config: EffectiveLlmConfig, provider: str) -> str:
    if provider == "openrouter":
        return config.openrouter_api_key
    if provider == "gemini":
        return config.gemini_api_key
    return ""


def _not_configured_detail(provider: str) -> str:
    """Italian 'provider not configured' detail (Ollama lacks an API key)."""
    if provider == "ollama":
        return _OLLAMA_NOT_CONFIGURED
    return f"Provider {provider} non configurato (chiave API mancante)."


def _ollama_base(config: EffectiveLlmConfig) -> str:
    """The effective Ollama base URL, trailing slash stripped (never a secret)."""
    return config.ollama_base_url.strip().rstrip("/")


def _ollama_unreachable_detail(config: EffectiveLlmConfig) -> str:
    return f"Ollama non raggiungibile su {_ollama_base(config)}. Verifica che sia in esecuzione."


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
# Ollama (local models: installed listing, downloadable catalog, in-app pull)
# --------------------------------------------------------------------------- #


#: Curated, downloadable Ollama models well-suited to structured-JSON analysis.
#: Static (no network) — the ``installed`` flag is computed per request against
#: the models actually present on the local server. Size hints are approximate
#: download sizes for the default quantization.
_OLLAMA_CATALOG: tuple[dict[str, str], ...] = (
    {
        "id": "llama3.2:3b",
        "label": "Llama 3.2 3B",
        "description_it": "Compatto e veloce di Meta: adatto anche a hardware modesto.",
        "size_hint": "~2 GB",
    },
    {
        "id": "llama3.1:8b",
        "label": "Llama 3.1 8B",
        "description_it": "Buon equilibrio tra qualità e velocità per analisi generali.",
        "size_hint": "~4.7 GB",
    },
    {
        "id": "qwen2.5:7b",
        "label": "Qwen2.5 7B",
        "description_it": "Modello Alibaba molto solido nella produzione di JSON strutturato.",
        "size_hint": "~4.7 GB",
    },
    {
        "id": "qwen2.5:14b",
        "label": "Qwen2.5 14B",
        "description_it": "Qwen2.5 più capace, migliore nel ragionamento numerico.",
        "size_hint": "~9 GB",
    },
    {
        "id": "gemma2:9b",
        "label": "Gemma 2 9B",
        "description_it": "Modello Google affidabile per riassunti e classificazione.",
        "size_hint": "~5.4 GB",
    },
    {
        "id": "gemma3:4b",
        "label": "Gemma 3 4B",
        "description_it": "Gemma 3 leggero, adatto a macchine con poca memoria.",
        "size_hint": "~3.3 GB",
    },
    {
        "id": "gemma3:12b",
        "label": "Gemma 3 12B",
        "description_it": "Gemma 3 di fascia media, buona qualità complessiva.",
        "size_hint": "~8.1 GB",
    },
    {
        "id": "mistral:7b",
        "label": "Mistral 7B",
        "description_it": "Classico 7B rapido ed efficiente per attività comuni.",
        "size_hint": "~4.1 GB",
    },
    {
        "id": "phi4:14b",
        "label": "Phi-4 14B",
        "description_it": "Modello Microsoft forte nel ragionamento pur restando contenuto.",
        "size_hint": "~9.1 GB",
    },
    {
        "id": "deepseek-r1:8b",
        "label": "DeepSeek-R1 8B",
        "description_it": "Modello di ragionamento, utile per analisi passo-passo.",
        "size_hint": "~5.2 GB",
    },
    {
        "id": "deepseek-r1:14b",
        "label": "DeepSeek-R1 14B",
        "description_it": "DeepSeek-R1 più grande, ragionamento più accurato.",
        "size_hint": "~9 GB",
    },
    {
        "id": "llama3.3:70b",
        "label": "Llama 3.3 70B",
        "description_it": "Modello di punta: massima qualità ma richiede molta memoria.",
        "size_hint": "~43 GB",
    },
    {
        "id": "qwen2.5:32b",
        "label": "Qwen2.5 32B",
        "description_it": "Qwen2.5 di grandi dimensioni per analisi complesse.",
        "size_hint": "~20 GB",
    },
    {
        "id": "mixtral:8x7b",
        "label": "Mixtral 8x7B",
        "description_it": "Mixture-of-experts potente, buon compromesso qualità/velocità.",
        "size_hint": "~26 GB",
    },
)


async def _check_ollama_reachable(base_url: str) -> None:
    """Raise if the local Ollama server does not answer ``GET /api/version``."""
    base = base_url.strip().rstrip("/")
    async with httpx.AsyncClient(timeout=_OLLAMA_PROBE_TIMEOUT_S) as client:
        response = await client.get(f"{base}/api/version")
        response.raise_for_status()


async def _fetch_ollama_tags(base_url: str) -> list[dict]:
    """Return the raw installed-model dicts from Ollama's ``GET /api/tags``."""
    base = base_url.strip().rstrip("/")
    async with httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT_S) as client:
        response = await client.get(f"{base}/api/tags")
        response.raise_for_status()
        data = response.json()
    items = data.get("models") if isinstance(data, dict) else None
    return [item for item in (items or []) if isinstance(item, dict)]


def _tag_name_and_label(item: dict) -> tuple[str, str] | None:
    """Extract ``(name, label)`` from one /api/tags entry, or None if unusable."""
    name = item.get("name") or item.get("model")
    if not isinstance(name, str) or not name:
        return None
    details = item.get("details")
    param_size = details.get("parameter_size") if isinstance(details, dict) else None
    label = f"{name} ({param_size})" if isinstance(param_size, str) and param_size else name
    return name, str(label)


def _installed_models_from_tags(tags: list[dict]) -> list[LlmModelInfo]:
    """Installed models as selectable ``LlmModelInfo`` (for GET /models)."""
    models: list[LlmModelInfo] = []
    for item in tags:
        parsed = _tag_name_and_label(item)
        if parsed is None:
            continue
        name, label = parsed
        models.append(LlmModelInfo(id=name, label=label))
    models.sort(key=lambda model: model.id)
    return models


def _installed_library_from_tags(tags: list[dict]) -> list[OllamaLibraryInstalled]:
    """Installed models with on-disk size (for GET /ollama/library)."""
    installed: list[OllamaLibraryInstalled] = []
    for item in tags:
        parsed = _tag_name_and_label(item)
        if parsed is None:
            continue
        name, label = parsed
        size = item.get("size")
        size_bytes = int(size) if isinstance(size, (int, float)) else None
        installed.append(OllamaLibraryInstalled(id=name, label=label, size_bytes=size_bytes))
    installed.sort(key=lambda model: model.id)
    return installed


# --- In-app model download (pull) -------------------------------------------- #
#
# Ollama's ``POST /api/pull`` streams NDJSON progress lines. We run one pull per
# model as a background asyncio task and expose its progress via a status
# endpoint. Concurrent pulls of DIFFERENT models are allowed; a second pull of a
# model already downloading is rejected (409). State is per-process (module-level)
# and guarded by a plain threading lock — mutations are trivial and synchronous.

_pull_lock = threading.Lock()
_PULL_STATES: dict[str, dict] = {}
#: Live task handles, kept only so the tasks are not garbage-collected mid-flight.
_PULL_TASKS: dict[str, asyncio.Task] = {}


def _new_pull_state() -> dict:
    return {
        "status": "pulling",
        "completed_bytes": None,
        "total_bytes": None,
        "percent": None,
        "detail_it": "Download avviato.",
    }


def _humanize_pull_status(status: str) -> str:
    """Turn an Ollama pull status string into an Italian progress line."""
    mapping = {
        "pulling manifest": "Lettura del manifest…",
        "verifying sha256 digest": "Verifica del checksum…",
        "writing manifest": "Scrittura del manifest…",
        "removing any unused layers": "Pulizia dei layer inutilizzati…",
        "success": "Download completato.",
    }
    if status in mapping:
        return mapping[status]
    if status.startswith(("pulling", "downloading")):
        return "Download in corso…"
    return f"Download in corso: {status}"


async def _stream_ollama_pull(base_url: str, model: str) -> AsyncIterator[dict]:
    """Yield each JSON progress line from ``POST /api/pull`` (streaming)."""
    base = base_url.strip().rstrip("/")
    # No READ timeout: minutes can elapse between progress lines on a big model;
    # the connect timeout still bounds the initial handshake.
    timeout = httpx.Timeout(_UPSTREAM_TIMEOUT_S, read=None)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST", f"{base}/api/pull", json={"name": model, "stream": True}
        ) as response:
            response.raise_for_status()
            async for raw in response.aiter_lines():
                line = raw.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except ValueError:
                    continue
                if isinstance(parsed, dict):
                    yield parsed


async def _run_pull(base_url: str, model: str) -> None:
    """Background task: stream a pull and keep ``_PULL_STATES[model]`` updated."""
    saw_success = False
    try:
        async for line in _stream_ollama_pull(base_url, model):
            status = line.get("status")
            total = line.get("total")
            completed = line.get("completed")
            error = line.get("error")
            with _pull_lock:
                state = _PULL_STATES.setdefault(model, _new_pull_state())
                if error:
                    state["status"] = "error"
                    state["detail_it"] = f"Errore durante il download: {error}"
                    return
                if isinstance(total, (int, float)):
                    state["total_bytes"] = int(total)
                if isinstance(completed, (int, float)):
                    state["completed_bytes"] = int(completed)
                tb, cb = state["total_bytes"], state["completed_bytes"]
                if isinstance(tb, int) and tb > 0 and isinstance(cb, int):
                    state["percent"] = round(min(cb / tb * 100.0, 100.0), 1)
                if status == "success":
                    saw_success = True
                    state["status"] = "success"
                    state["percent"] = 100.0
                    state["detail_it"] = "Download completato."
                elif isinstance(status, str) and status:
                    state["status"] = "pulling"
                    state["detail_it"] = _humanize_pull_status(status)
        # A clean end without an explicit "success" line still counts as done.
        with _pull_lock:
            state = _PULL_STATES.setdefault(model, _new_pull_state())
            if not saw_success and state["status"] == "pulling":
                state["status"] = "success"
                state["percent"] = 100.0
                state["detail_it"] = "Download completato."
    except Exception as exc:  # network drop, HTTP error, etc.
        logger.warning("Download Ollama del modello %s fallito: %s", model, exc)
        with _pull_lock:
            state = _PULL_STATES.setdefault(model, _new_pull_state())
            state["status"] = "error"
            state["detail_it"] = f"Errore durante il download: {exc}"


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
        raise HTTPException(status_code=400, detail=_not_configured_detail(provider))

    # Ollama is a cheap local call: no TTL cache, and the "models" are whatever is
    # currently installed on the server (GET /api/tags).
    if provider == "ollama":
        try:
            tags = await _fetch_ollama_tags(config.ollama_base_url)
        except Exception as exc:
            logger.warning("Recupero modelli da ollama fallito: %s", exc)
            raise HTTPException(
                status_code=502,
                detail="Impossibile recuperare l'elenco dei modelli da ollama.",
            ) from exc
        return LlmModelsOut(
            provider=provider,
            models=_installed_models_from_tags(tags),
            fetched_at=datetime.utcnow(),
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
            raise HTTPException(status_code=400, detail=_not_configured_detail(ref.provider))
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
                base_url=None,
            ),
            LlmProviderState(
                provider="gemini",
                configured=config.gemini_configured,
                source=config.gemini_source,
                key_masked=(
                    _mask_key(config.gemini_api_key) if config.gemini_configured else None
                ),
                default_model=config.gemini_model,
                base_url=None,
            ),
            # Ollama is keyless: ``key_masked`` is always null; ``base_url`` carries
            # the effective server URL (null when Ollama is disabled).
            LlmProviderState(
                provider="ollama",
                configured=config.ollama_configured,
                source=config.ollama_source,
                key_masked=None,
                default_model=config.ollama_model,
                base_url=config.ollama_base_url or None,
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
    if "ollama_base_url" in fields and payload.ollama_base_url is not None:
        trimmed = payload.ollama_base_url.strip()
        if not trimmed.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="URL di Ollama non valido.")

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
    if "ollama_base_url" in fields:
        row.ollama_base_url = (
            payload.ollama_base_url.strip() if payload.ollama_base_url is not None else None
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

    # Ollama has no API key: "reachable" means GET /api/version answers.
    if provider == "ollama":
        config = get_effective()
        if not config.ollama_configured:
            raise HTTPException(status_code=400, detail=_OLLAMA_NOT_CONFIGURED)
        try:
            await _check_ollama_reachable(config.ollama_base_url)
        except Exception as exc:  # connection/timeout/HTTP errors
            logger.warning("Test del provider ollama fallito: %s", exc)
            return LlmProviderTestResult(ok=False, detail_it=_ollama_unreachable_detail(config))
        return LlmProviderTestResult(ok=True, detail_it="Connessione riuscita: Ollama raggiungibile.")

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


# --------------------------------------------------------------------------- #
# Ollama library + in-app model download
# --------------------------------------------------------------------------- #


@router.get("/ollama/library", response_model=OllamaLibraryOut)
async def ollama_library() -> OllamaLibraryOut:
    """Return the locally-installed models plus the curated downloadable catalog.

    400 when Ollama is not configured; 502 when the local server is unreachable.
    Each catalog entry is flagged ``installed`` when its id (or its family prefix
    before ``:``) matches a model already present on the server.
    """
    config = get_effective()
    if not config.ollama_configured:
        raise HTTPException(status_code=400, detail=_OLLAMA_NOT_CONFIGURED)
    try:
        tags = await _fetch_ollama_tags(config.ollama_base_url)
    except Exception as exc:
        logger.warning("Recupero libreria Ollama fallito: %s", exc)
        raise HTTPException(status_code=502, detail=_ollama_unreachable_detail(config)) from exc

    installed = _installed_library_from_tags(tags)
    installed_ids = {model.id for model in installed}
    installed_prefixes = {model.id.split(":", 1)[0] for model in installed}
    catalog = [
        OllamaCatalogEntry(
            id=entry["id"],
            label=entry["label"],
            description_it=entry["description_it"],
            size_hint=entry["size_hint"],
            installed=(
                entry["id"] in installed_ids
                or entry["id"].split(":", 1)[0] in installed_prefixes
            ),
        )
        for entry in _OLLAMA_CATALOG
    ]
    return OllamaLibraryOut(installed=installed, catalog=catalog)


@router.post("/ollama/pull", status_code=202)
async def ollama_pull(payload: OllamaPullRequest) -> dict:
    """Start an in-app download of an Ollama model (background streaming pull).

    202 with ``{"detail": "Download avviato."}`` when the pull starts; 409 when a
    pull of the SAME model is already running; 400 for an empty model or when
    Ollama is not configured; 502 when the server is unreachable. Progress is then
    polled via ``GET /ollama/pull/status``. Pulls of DIFFERENT models may run
    concurrently.
    """
    config = get_effective()
    model = (payload.model or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="Specificare un modello da scaricare.")
    if not config.ollama_configured:
        raise HTTPException(status_code=400, detail=_OLLAMA_NOT_CONFIGURED)

    with _pull_lock:
        existing = _PULL_STATES.get(model)
        if existing is not None and existing["status"] == "pulling":
            raise HTTPException(
                status_code=409, detail="Download già in corso per questo modello."
            )

    # Fail fast with a synchronous 502 if the server is down, rather than letting
    # the user discover the failure only by polling the background task's status.
    try:
        await _check_ollama_reachable(config.ollama_base_url)
    except Exception as exc:
        logger.warning("Avvio download Ollama fallito (server non raggiungibile): %s", exc)
        raise HTTPException(status_code=502, detail=_ollama_unreachable_detail(config)) from exc

    with _pull_lock:
        existing = _PULL_STATES.get(model)
        if existing is not None and existing["status"] == "pulling":
            raise HTTPException(
                status_code=409, detail="Download già in corso per questo modello."
            )
        _PULL_STATES[model] = _new_pull_state()

    _PULL_TASKS[model] = asyncio.create_task(_run_pull(config.ollama_base_url, model))
    return {"detail": "Download avviato."}


@router.get("/ollama/pull/status", response_model=OllamaPullStatusOut)
def ollama_pull_status(model: str) -> OllamaPullStatusOut:
    """Report the progress of an in-app Ollama download (``idle`` when never started)."""
    with _pull_lock:
        state = _PULL_STATES.get(model)
        if state is None:
            return OllamaPullStatusOut(
                model=model, status="idle", detail_it="Nessun download in corso."
            )
        return OllamaPullStatusOut(
            model=model,
            status=state["status"],
            completed_bytes=state["completed_bytes"],
            total_bytes=state["total_bytes"],
            percent=state["percent"],
            detail_it=state["detail_it"],
        )
