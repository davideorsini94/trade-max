"""Shared FastAPI dependencies for the API layer.

Re-exports the request-scoped DB session dependency from ``app.db`` (so every
router imports it from a single place, ``app.api.deps``, per the blueprint
file tree) and adds a process-wide cached accessor for the ``LLMClient``.

Building the ``LLMClient`` is intentionally lazy and cached: it reuses the same
provider objects (and their pooled ``httpx`` clients) across requests instead of
reconstructing them on every call. The single client stays current by re-syncing
its providers from the *effective* LLM config (env + DB overrides) whenever that
config's generation changes — so a provider/key change saved from the app's
Settings page takes effect on the very next LLM call with no restart.
"""

from __future__ import annotations

from app.db import get_db
from app.llm.client import LLMClient

__all__ = ["get_db", "get_llm_client"]

_llm_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    """Return the process-wide singleton ``LLMClient`` (self-syncs to config changes)."""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client
