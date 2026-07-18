"""Effective LLM runtime configuration — the single source of truth.

Merges the env-configured :class:`~app.config.Settings` (``get_settings()``) with
the DB overrides stored in the single-row ``llm_provider_settings`` table: for
each field a non-null DB value wins, otherwise the env value is used. Models are
NOT overridable here — they always come from env / the ``llm_model_prefs`` table.

Reads are cached process-wide behind a monotonic *generation* counter. Any write
to the provider settings (``PUT /api/llm/providers``) calls
:func:`invalidate_effective`, which bumps the generation and drops the cache, so
the next :func:`get_effective` rebuilds from fresh state and — via the generation
— the long-lived :class:`~app.llm.client.LLMClient` re-syncs its providers.

Every DB access is defensive: any error yields a pure-env config so a missing
table or a locked database can never break the pipeline. API keys are secrets;
this module never logs their values.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Literal

from app.config import get_settings
from app.db import session_scope
from app.models import LlmProviderSettings

logger = logging.getLogger(__name__)

Source = Literal["app", "env"] | None


@dataclass(frozen=True)
class EffectiveLlmConfig:
    """The LLM configuration in force right now (env with DB overrides applied)."""

    primary_provider: str
    openrouter_api_key: str
    gemini_api_key: str
    #: Local Ollama base URL ("" => Ollama disabled). Not a secret.
    ollama_base_url: str
    openrouter_model: str
    gemini_model: str
    ollama_model: str
    fallback_enabled: bool
    #: Where each effective key/URL came from: "app" (DB), "env" (.env), or None (absent).
    openrouter_source: Source
    gemini_source: Source
    ollama_source: Source

    @property
    def openrouter_configured(self) -> bool:
        return bool(self.openrouter_api_key.strip())

    @property
    def gemini_configured(self) -> bool:
        return bool(self.gemini_api_key.strip())

    @property
    def ollama_configured(self) -> bool:
        return bool(self.ollama_base_url.strip())


# Cache guarded by ``_lock``; ``_generation`` is bumped on every invalidation and
# ``_cached_generation`` records which generation ``_cache`` was computed against.
_lock = threading.Lock()
_cache: EffectiveLlmConfig | None = None
_cached_generation: int = -1
_generation: int = 0


def _resolve_key(db_value: str | None, env_value: str) -> tuple[str, Source]:
    """Pick the effective key + its source (DB app-key wins over env)."""
    if db_value is not None and db_value.strip():
        return db_value.strip(), "app"
    if env_value.strip():
        return env_value, "env"
    return "", None


def _compute_effective() -> EffectiveLlmConfig:
    """Build the effective config from env + the DB provider-settings row.

    Any DB error degrades cleanly to a pure-env config (the row values default to
    ``None``, i.e. "no override").
    """
    settings = get_settings()
    env_openrouter = settings.openrouter_api_key or ""
    env_gemini = settings.gemini_api_key or ""
    env_ollama = settings.ollama_base_url or ""
    env_primary = settings.llm_provider
    env_fallback = settings.llm_fallback_enabled

    db_primary: str | None = None
    db_openrouter: str | None = None
    db_gemini: str | None = None
    db_ollama: str | None = None
    db_fallback: bool | None = None
    try:
        with session_scope() as db:
            row = db.get(LlmProviderSettings, 1)
            if row is not None:
                db_primary = row.primary_provider
                db_openrouter = row.openrouter_api_key
                db_gemini = row.gemini_api_key
                db_ollama = row.ollama_base_url
                db_fallback = row.fallback_enabled
    except Exception:  # pragma: no cover - defensive
        logger.warning(
            "Lettura impostazioni provider LLM dal DB fallita; uso solo .env", exc_info=True
        )
        db_primary = db_openrouter = db_gemini = db_ollama = db_fallback = None

    openrouter_key, openrouter_source = _resolve_key(db_openrouter, env_openrouter)
    gemini_key, gemini_source = _resolve_key(db_gemini, env_gemini)
    # The Ollama base URL merges DB-over-env exactly like the API keys, even though
    # it is not a secret ("app" when saved in the DB, "env" from .env, else None).
    ollama_url, ollama_source = _resolve_key(db_ollama, env_ollama)

    primary = db_primary if (db_primary and db_primary.strip()) else env_primary
    primary = (primary or "openrouter").strip().lower()
    fallback = env_fallback if db_fallback is None else db_fallback

    return EffectiveLlmConfig(
        primary_provider=primary,
        openrouter_api_key=openrouter_key,
        gemini_api_key=gemini_key,
        ollama_base_url=ollama_url,
        openrouter_model=settings.openrouter_model,
        gemini_model=settings.gemini_model,
        ollama_model=settings.ollama_model,
        fallback_enabled=bool(fallback),
        openrouter_source=openrouter_source,
        gemini_source=gemini_source,
        ollama_source=ollama_source,
    )


def get_generation() -> int:
    """Current config generation; changes whenever the effective config is invalidated."""
    with _lock:
        return _generation


def get_effective() -> EffectiveLlmConfig:
    """Return the cached effective config, recomputing it after an invalidation."""
    global _cache, _cached_generation
    with _lock:
        gen = _generation
        if _cache is not None and _cached_generation == gen:
            return _cache
    # Compute outside the lock so a slow DB read never blocks other callers.
    fresh = _compute_effective()
    with _lock:
        # Publish only if no invalidation raced us; otherwise return the fresh
        # value uncached (the next call recomputes against the newer generation).
        if _generation == gen:
            _cache = fresh
            _cached_generation = gen
        return fresh


def invalidate_effective() -> None:
    """Drop the cached config and bump the generation (call after any write)."""
    global _generation, _cache, _cached_generation
    with _lock:
        _generation += 1
        _cache = None
        _cached_generation = -1
