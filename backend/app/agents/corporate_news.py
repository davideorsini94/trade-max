"""Corporate news analyst agent (blueprint 5.4).

Evaluates company-specific catalysts — earnings, guidance, regulatory filings,
M&A, litigation, and public statements by executives or influential figures —
from the per-ticker news items provided in the context.
"""

from __future__ import annotations

from typing import Any

from app.agents.base import (
    AgentContext,
    BaseAgent,
    coerce_enum,
    coerce_str,
    compact_json,
    symbol_descriptor,
)

IMPACT_VALUES: frozenset[str] = frozenset({"POSITIVE", "NEGATIVE", "MIXED"})
MAX_CATALYSTS: int = 5

_SYSTEM_BODY = """\
You are a corporate news analyst on a CONSERVATIVE advisory desk. You evaluate \
company-specific catalysts for THIS symbol over the next ~30 days from ONLY the news \
items provided in the user message. These items come from a curated whitelist \
(the company's Yahoo Finance headline feed, SEC EDGAR 8-K filings, and CNBC business \
news) and may reference the company itself, closely related large groups, and public \
statements by executives, well-known investors, or policymakers.

What to look for:
- Earnings results and forward guidance (beats/misses, raised/cut outlook).
- Regulatory filings and disclosures (8-K events, investigations).
- M&A, partnerships, major contracts, product launches or recalls.
- Litigation, fines, management changes.
- Public statements by influential figures quoted in the items.
- The provided calendar's next_earnings_date and days_to_earnings fields: a \
scheduled earnings report within the horizon is a RISK to flag, not a realized \
catalyst.

Source weighting:
- Regulatory filings (SEC EDGAR) are more reliable than press articles; press is more \
reliable than opinion. Weight your conviction accordingly and be skeptical of \
rumor-grade or clickbait items.

Strict rules:
- Use ONLY the provided items. Do NOT recall or assume news that is not in the payload.
- If the items are few, stale, or not clearly about this company, stay NEUTRAL with \
LOW confidence and mark data_quality PARTIAL or POOR.
- Distinguish confirmed events from speculation; a scheduled but not-yet-released \
earnings report is a RISK, not a realized catalyst.
- signal: -1.0 (strongly negative company-specific news flow) to +1.0 (strongly \
positive); 0.0 for neutral. confidence in [0.0, 1.0].

Output STRICT JSON and NOTHING else — no markdown, no code fences, no text outside \
the single JSON object. It MUST match exactly this schema:
{
  "stance": "BULLISH" | "BEARISH" | "NEUTRAL",
  "signal": <number in [-1.0, 1.0]>,
  "confidence": <number in [0.0, 1.0]>,
  "catalysts": [
    {"event": "<short English description>", "impact": "POSITIVE" | "NEGATIVE" | "MIXED"}
  ],
  "key_points": [<at most 5 short English strings>],
  "risks": [<at most 3 short English strings>],
  "data_quality": "GOOD" | "PARTIAL" | "POOR",
  "summary_it": "<2-4 sentences in ITALIAN addressed to the end user>"
}
All keys are required (use an empty array for catalysts if there are none). \
catalysts, key_points and risks are in English; summary_it is in Italian. Do not add \
keys that are not in the schema."""


def _coerce_catalysts(value: Any) -> list[dict[str, str]]:
    """Coerce the ``catalysts`` field into a clean list of {event, impact} dicts."""
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        event = coerce_str(item.get("event"), "")
        if not event:
            continue
        impact = coerce_enum(item.get("impact"), IMPACT_VALUES, "MIXED")
        result.append({"event": event, "impact": impact})
        if len(result) >= MAX_CATALYSTS:
            break
    return result


class CorporateNewsAnalystAgent(BaseAgent):
    """Evaluates company-specific catalysts and statements from curated feeds."""

    name = "corporate_news"
    temperature = 0.25
    # Analyst JSON is ~300-500 tokens; 900 leaves ample headroom while cutting cost.
    max_tokens = 900

    def _system_body(self) -> str:
        return _SYSTEM_BODY

    def build_user_prompt(self, ctx: AgentContext) -> str:
        payload: dict[str, Any] = {
            "symbol": symbol_descriptor(ctx.symbol),
            "corporate_news": ctx.corporate_news,
            "calendar": ctx.calendar,
        }
        return (
            "Evaluate the company-specific catalysts in the following curated items "
            "and respond with the JSON object described in your instructions.\n"
            + compact_json(payload)
        )

    def validate_output(self, data: dict) -> dict:
        out = super().validate_output(data)
        raw = data if isinstance(data, dict) else {}
        out["catalysts"] = _coerce_catalysts(raw.get("catalysts"))
        return out
