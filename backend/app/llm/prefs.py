"""Per-agent / default LLM model preferences with a short-lived cache.

Reads the ``llm_model_prefs`` table (populated by ``PUT /api/llm/config``) and
resolves the effective ``(provider, model)`` for any actor: an agent-specific row
wins, otherwise the ``default`` row, otherwise ``None`` (callers then fall back to
the env-configured provider/model).

The table is tiny and read on every LLM call, so results are cached process-wide
for :data:`_CACHE_TTL_S` seconds; ``PUT`` calls :func:`invalidate_prefs_cache`.
Every DB access is defensive: any error resolves to ``None`` so a missing table
or a locked database can never break the pipeline — it just degrades to env config.
"""

from __future__ import annotations

import logging
import threading
import time

from app.db import session_scope
from app.models import LlmModelPref

logger = logging.getLogger(__name__)

#: How long a loaded snapshot of the prefs table stays valid.
_CACHE_TTL_S = 60.0

_lock = threading.Lock()
_cache: dict[str, tuple[str, str]] | None = None
_loaded_monotonic: float = 0.0


def _load_all() -> dict[str, tuple[str, str]]:
    """Read every pref row as ``{agent_name: (provider, model)}`` (may raise)."""
    with session_scope() as db:
        rows = db.query(LlmModelPref).all()
        return {row.agent_name: (row.provider, row.model) for row in rows}


def _get_cached_prefs() -> dict[str, tuple[str, str]]:
    """Return the cached prefs snapshot, reloading it when the TTL has expired."""
    global _cache, _loaded_monotonic
    now = time.monotonic()
    with _lock:
        if _cache is not None and (now - _loaded_monotonic) < _CACHE_TTL_S:
            return _cache
    # Load outside the lock so a slow DB read never blocks other threads on it.
    fresh = _load_all()
    with _lock:
        _cache = fresh
        _loaded_monotonic = time.monotonic()
        return _cache


def get_pref(agent_name: str) -> tuple[str, str] | None:
    """Resolve ``(provider, model)`` for ``agent_name``.

    Resolution order: the agent's own row -> the ``default`` row -> ``None``.
    Any DB/read error is swallowed and resolves to ``None`` (fall back to env).
    """
    try:
        prefs = _get_cached_prefs()
    except Exception:  # pragma: no cover - defensive
        logger.warning("Lettura preferenze modello LLM fallita", exc_info=True)
        return None
    pref = prefs.get(agent_name)
    if pref is not None:
        return pref
    return prefs.get("default")


def invalidate_prefs_cache() -> None:
    """Drop the cached snapshot so the next :func:`get_pref` re-reads the DB."""
    global _cache, _loaded_monotonic
    with _lock:
        _cache = None
        _loaded_monotonic = 0.0
