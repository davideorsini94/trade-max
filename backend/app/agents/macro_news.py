"""Macro news analyst agent (blueprint 5.4).

Assesses how the macro environment (rates, inflation, geopolitics, sector policy)
affects the symbol over the next ~30 days, using ONLY the curated macro
headlines provided in the context.
"""

from __future__ import annotations

from typing import Any

from app.agents.base import (
    AgentContext,
    BaseAgent,
    coerce_str_list,
    compact_json,
    symbol_descriptor,
)

MAX_MACRO_DRIVERS: int = 5

_SYSTEM_BODY = """\
You are a macro strategist on a CONSERVATIVE advisory desk. From ONLY the provided \
headlines and summaries — which come from a curated whitelist of reputable sources \
(central banks such as the Federal Reserve and the ECB, regulators, and major \
financial press) — assess how the current macro environment affects THIS specific \
symbol over the next 30 days.

Consider the macro channels that plausibly transmit to the symbol: interest rates \
and central-bank policy, inflation, growth/recession signals, currency moves, \
commodity prices, geopolitics, and sector-specific policy or regulation. Use the \
symbol's currency, exchange, sector and industry (when provided) to judge relevance.

Strict rules:
- Use ONLY the provided items. Do NOT rely on outside knowledge of events, and do \
NOT assume anything about developments after your training cut-off. If an item is \
not in the payload, it does not exist for this analysis.
- If the news is scarce, stale, or only weakly related to the symbol, stay NEUTRAL \
with LOW confidence and mark data_quality PARTIAL or POOR.
- Distinguish macro tailwinds from headwinds for THIS symbol specifically, not for \
the market in general.
- signal: -1.0 (macro backdrop is a strong headwind for the symbol) to +1.0 (strong \
tailwind); 0.0 for neutral/irrelevant. confidence in [0.0, 1.0].

Output STRICT JSON and NOTHING else — no markdown, no code fences, no text outside \
the single JSON object. It MUST match exactly this schema:
{
  "stance": "BULLISH" | "BEARISH" | "NEUTRAL",
  "signal": <number in [-1.0, 1.0]>,
  "confidence": <number in [0.0, 1.0]>,
  "macro_drivers": [<at most 5 short English strings naming the key macro forces>],
  "key_points": [<at most 5 short English strings>],
  "risks": [<at most 3 short English strings>],
  "data_quality": "GOOD" | "PARTIAL" | "POOR",
  "summary_it": "<2-4 sentences in ITALIAN addressed to the end user>"
}
All keys are required. macro_drivers, key_points and risks are in English; \
summary_it is in Italian. Do not add keys that are not in the schema."""


class MacroNewsAnalystAgent(BaseAgent):
    """Judges the macro backdrop's impact on the symbol from curated headlines."""

    name = "macro_news"
    temperature = 0.25
    # Analyst JSON is ~300-500 tokens; 900 leaves ample headroom while cutting cost.
    max_tokens = 900

    def _system_body(self) -> str:
        return _SYSTEM_BODY

    def build_user_prompt(self, ctx: AgentContext) -> str:
        descriptor = symbol_descriptor(ctx.symbol)
        # Enrich the symbol block with sector/industry when the fundamentals
        # payload happens to carry them — this helps judge macro relevance
        # without giving the macro agent the full fundamentals dict.
        fundamentals = ctx.fundamentals if isinstance(ctx.fundamentals, dict) else {}
        for key in ("sector", "industry"):
            value = fundamentals.get(key)
            if value:
                descriptor[key] = value
        payload: dict[str, Any] = {
            "symbol": descriptor,
            "macro_news": ctx.macro_news,
        }
        return (
            "Assess the macro backdrop for this symbol from the following curated "
            "headlines and respond with the JSON object described in your "
            "instructions.\n" + compact_json(payload)
        )

    def validate_output(self, data: dict) -> dict:
        out = super().validate_output(data)
        raw = data if isinstance(data, dict) else {}
        out["macro_drivers"] = coerce_str_list(raw.get("macro_drivers"), MAX_MACRO_DRIVERS)
        return out
