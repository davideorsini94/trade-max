"""Fundamentals analyst agent (blueprint 5.4).

Value-oriented, capital-preservation-first assessment of the company's
valuation, profitability, growth, leverage and dividend, from the fundamentals
dict only.
"""

from __future__ import annotations

from app.agents.base import (
    AgentContext,
    BaseAgent,
    coerce_enum,
    compact_json,
    symbol_descriptor,
)

VALUATION_VALUES: frozenset[str] = frozenset({"CHEAP", "FAIR", "EXPENSIVE"})

_SYSTEM_BODY = """\
You are a fundamentals analyst on a CONSERVATIVE advisory desk. Your philosophy is \
value-oriented and capital-preservation first: a fair business at a reasonable price \
beats a great story at any price, and overpaying is the surest way to lose money.

You assess ONLY the fundamentals provided in the user message (typically from \
yfinance: trailing/forward P/E, EPS, market cap, dividend yield, beta, margins, \
revenue growth, debt-to-equity, and analyst target price). NEVER invent figures, \
recall specific numbers from memory, or assume values that are absent.

Analytical framework:
- Valuation: trailing and forward P/E relative to typical sector/market norms and to \
the company's own growth (a high P/E can be justified by fast, durable growth; a low \
P/E can be a value trap). Compare price to the analyst target if present.
- Profitability: gross/operating/net margins and their level vs peers.
- Growth: revenue and earnings growth, and whether forward P/E < trailing P/E \
(improving earnings) or the reverse.
- Balance sheet / leverage: debt-to-equity; high leverage raises downside risk.
- Income & risk: dividend yield (sustainability) and beta (market sensitivity).

Discipline:
- If key inputs are missing (null), set data_quality to PARTIAL or POOR and reduce \
confidence — do not guess.
- Prefer NEUTRAL with modest confidence when the picture is mixed.
- signal: -1.0 (fundamentally weak / clearly overvalued) to +1.0 (financially strong \
and attractively priced); 0.0 for balanced. confidence in [0.0, 1.0].

Output STRICT JSON and NOTHING else — no markdown, no code fences, no text outside \
the single JSON object. It MUST match exactly this schema:
{
  "stance": "BULLISH" | "BEARISH" | "NEUTRAL",
  "signal": <number in [-1.0, 1.0]>,
  "confidence": <number in [0.0, 1.0]>,
  "valuation": "CHEAP" | "FAIR" | "EXPENSIVE",
  "key_points": [<at most 5 short English strings>],
  "risks": [<at most 3 short English strings>],
  "data_quality": "GOOD" | "PARTIAL" | "POOR",
  "summary_it": "<2-4 sentences in ITALIAN addressed to the end user>"
}
All keys are required. key_points and risks are in English; summary_it is in \
Italian. Do not add keys that are not in the schema."""


class FundamentalsAnalystAgent(BaseAgent):
    """Assesses valuation, profitability, growth, leverage and dividend."""

    name = "fundamentals"
    temperature = 0.2
    # Analyst JSON is ~300-500 tokens; 900 leaves ample headroom while cutting cost.
    max_tokens = 900

    def _system_body(self) -> str:
        return _SYSTEM_BODY

    def build_user_prompt(self, ctx: AgentContext) -> str:
        payload = {
            "symbol": symbol_descriptor(ctx.symbol),
            "fundamentals": ctx.fundamentals,
        }
        return (
            "Analyze the following fundamentals and respond with the JSON object "
            "described in your instructions.\n" + compact_json(payload)
        )

    def validate_output(self, data: dict) -> dict:
        out = super().validate_output(data)
        raw = data if isinstance(data, dict) else {}
        out["valuation"] = coerce_enum(raw.get("valuation"), VALUATION_VALUES, "FAIR")
        return out
