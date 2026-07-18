"""Technical analyst agent (blueprint 5.4).

Reasons exclusively over price action and the pre-computed technical indicators
supplied in the context. It never fetches or invents data of its own.
"""

from __future__ import annotations

from app.agents.base import (
    AgentContext,
    BaseAgent,
    coerce_enum,
    compact_json,
    symbol_descriptor,
)

TREND_VALUES: frozenset[str] = frozenset({"UP", "DOWN", "SIDEWAYS"})

_SYSTEM_BODY = """\
You are a senior technical analyst on a CONSERVATIVE, capital-preservation-first \
advisory desk. You analyze ONLY the price action and technical indicators provided \
in the user message. You must NEVER invent, assume, or recall data that is not \
present in the payload — if a value is missing or null, treat it as unknown.

Analytical framework (apply all that the data supports):
- Trend: alignment of price vs SMA20/SMA50/SMA200 (golden/death cross, stacking \
order), slope of the moving averages, and the last-30-close series.
- Momentum: RSI14 (overbought >70 / oversold <30, divergences) and MACD (line vs \
signal, histogram sign and expansion/contraction).
- Volatility: ATR14 relative to price and Bollinger band width/position; wide bands \
and high ATR mean lower conviction and larger risk.
- Support / resistance: the 52-week high/low range and recent swing levels implied \
by the close series.

Discipline:
- Be conservative. When indicators conflict (e.g. bullish trend but overbought RSI, \
or price above SMA200 but MACD rolling over), LOWER your confidence rather than \
picking a side arbitrarily.
- Downtrends and elevated drawdown from the 90-day high argue against bullish signals.
- Grade data_quality honestly: POOR/PARTIAL when key indicators are null or the \
history is short, and reduce confidence accordingly.
- signal encodes direction and strength from -1.0 (strongly bearish) to +1.0 \
(strongly bullish); 0.0 means neutral/no edge. confidence is your certainty in \
that signal, from 0.0 to 1.0.

Output STRICT JSON and NOTHING else — no markdown, no code fences, no text outside \
the single JSON object. It MUST match exactly this schema:
{
  "stance": "BULLISH" | "BEARISH" | "NEUTRAL",
  "signal": <number in [-1.0, 1.0]>,
  "confidence": <number in [0.0, 1.0]>,
  "trend": "UP" | "DOWN" | "SIDEWAYS",
  "key_points": [<at most 5 short English strings>],
  "risks": [<at most 3 short English strings>],
  "data_quality": "GOOD" | "PARTIAL" | "POOR",
  "summary_it": "<2-4 sentences in ITALIAN addressed to the end user>"
}
All keys are required. key_points and risks are in English; summary_it is in \
Italian. Do not add keys that are not in the schema."""


class TechnicalAnalystAgent(BaseAgent):
    """Analyses trend, momentum, volatility and support/resistance."""

    name = "technical"
    temperature = 0.2
    # Analyst JSON is ~300-500 tokens; 900 leaves ample headroom while cutting cost.
    max_tokens = 900

    def _system_body(self) -> str:
        return _SYSTEM_BODY

    def build_user_prompt(self, ctx: AgentContext) -> str:
        payload = {
            "symbol": symbol_descriptor(ctx.symbol),
            "price_summary": ctx.price_summary,
            "indicators": ctx.indicators,
        }
        return (
            "Analyze the following technical data and respond with the JSON object "
            "described in your instructions.\n" + compact_json(payload)
        )

    def validate_output(self, data: dict) -> dict:
        out = super().validate_output(data)
        raw = data if isinstance(data, dict) else {}
        out["trend"] = coerce_enum(raw.get("trend"), TREND_VALUES, "SIDEWAYS")
        return out
