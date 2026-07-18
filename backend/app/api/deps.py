"""Shared FastAPI dependencies for the API layer.

Re-exports the request-scoped DB session dependency from ``app.db`` (so every
router imports it from a single place, ``app.api.deps``, per the blueprint
file tree) and adds a process-wide cached accessor for the ``LLMClient``.

Building the ``LLMClient`` is intentionally lazy and cached: it reads
``Settings`` once and reuses the same provider objects (and their pooled
``httpx`` clients) across requests instead of reconstructing them on every
call.
"""

from __future__ import annotations

from app.config import get_settings
from app.db import get_db
from app.llm.client import LLMClient

__all__ = ["get_db", "get_llm_client"]

_llm_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    """Return a process-wide singleton ``LLMClient`` built from current settings."""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient(get_settings())
    return _llm_client
