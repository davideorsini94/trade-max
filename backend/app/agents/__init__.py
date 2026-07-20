"""Multi-agent pipeline actors (blueprint section 5).

Exposes the six agents, the shared data contracts, and ``ANALYST_AGENTS`` — the
ordered registry of the four analyst instances the orchestrator fans out over
with ``asyncio.gather``. The synthesizer and validator run afterwards on the
aggregated results and are therefore exported as classes rather than being part
of the fan-out registry.
"""

from __future__ import annotations

from app.agents.base import AgentContext, AgentResult, BaseAgent
from app.agents.corporate_news import CorporateNewsAnalystAgent
from app.agents.fundamentals import FundamentalsAnalystAgent
from app.agents.macro_news import MacroNewsAnalystAgent
from app.agents.sentiment import SentimentAnalystAgent
from app.agents.synthesizer import SynthesizerAgent
from app.agents.technical import TechnicalAnalystAgent
from app.agents.validator import RiskValidatorAgent

#: The five analyst agents, in canonical order, that the orchestrator runs
#: concurrently for every analysis run. Order matches ``synthesizer.ANALYST_KEYS``.
ANALYST_AGENTS: list[BaseAgent] = [
    TechnicalAnalystAgent(),
    FundamentalsAnalystAgent(),
    MacroNewsAnalystAgent(),
    CorporateNewsAnalystAgent(),
    SentimentAnalystAgent(),
]

__all__ = [
    "AgentContext",
    "AgentResult",
    "BaseAgent",
    "TechnicalAnalystAgent",
    "FundamentalsAnalystAgent",
    "MacroNewsAnalystAgent",
    "CorporateNewsAnalystAgent",
    "SentimentAnalystAgent",
    "SynthesizerAgent",
    "RiskValidatorAgent",
    "ANALYST_AGENTS",
]
