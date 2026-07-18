"""Decision engine: deterministic risk policy + analysis orchestrator (blueprint §5.5/§6).

``policy`` is pure, LLM-free code (the thirteen ordered risk rules);
``orchestrator`` drives the end-to-end multi-agent analysis pipeline for one
symbol. The submodules are imported lazily by their consumers (the API layer
imports ``app.engine.orchestrator`` directly), so this package initializer stays
intentionally minimal to avoid import cycles.
"""

from __future__ import annotations

__all__ = ["orchestrator", "policy"]
