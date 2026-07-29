"""Synthesizer agent (blueprint 5.4).

The chief investment strategist: combines the five analyst reports into a single
conservative, actionable proposal (action + sizing + allocation + levels +
rationale). Consumes aggregated inputs rather than a raw ``AgentContext``, so it
overrides ``run``/``build_user_prompt`` while reusing ``BaseAgent`` helpers and
the mandatory lessons block.
"""

from __future__ import annotations

from typing import Any

from app.agents.base import (
    AgentResult,
    BaseAgent,
    clamp,
    coerce_enum,
    coerce_float,
    coerce_int,
    coerce_optional_float,
    coerce_optional_str,
    coerce_str,
    compact_json,
    resolve_llm_pref,
)
from app.llm.client import LLMClient
from app.schemas import Action, Sizing

ACTION_VALUES: frozenset[str] = frozenset(a.value for a in Action)
SIZING_VALUES: frozenset[str] = frozenset(s.value for s in Sizing)

#: The five analyst slots the synthesizer expects, in canonical order.
ANALYST_KEYS: tuple[str, ...] = (
    "technical",
    "fundamentals",
    "macro_news",
    "corporate_news",
    "sentiment",
)

_DEFAULT_WEIGHTS: dict[str, float] = {
    "technical": 0.25,
    "fundamentals": 0.25,
    "macro_news": 0.15,
    "corporate_news": 0.15,
    "sentiment": 0.2,
}

_SYSTEM_BODY = """\
You are the chief investment strategist of a CONSERVATIVE advisory desk. Your \
non-negotiable priority is CAPITAL PRESERVATION: missing a gain is acceptable, taking \
a large loss is not. This is advisory only — you never execute orders.

You are given five analyst reports - technical, fundamentals, macro, corporate news, \
and market sentiment (analyst consensus, insider and institutional positioning, short \
interest). A report may be null when that analyst failed; simply weight it as absent and \
lean on the others (and lower overall confidence when coverage is thin).

How to combine them:
- Weight each analyst by its own confidence AND data_quality (GOOD > PARTIAL > POOR). \
Discount any analyst that the lessons below flag as historically unreliable.
- Look for agreement and for dissent. When the analysts genuinely conflict, prefer a \
cautious action (often HOLD) and a smaller size; record the disagreement in "dissent".
- Respect the requested risk profile and the budget context provided.
- Prefer GRADUAL entries (DCA or PARTIAL) over ALL_IN. Reserve ALL_IN for rare, \
high-conviction, low-volatility setups — and even then a conservative desk usually \
avoids it. Use WAIT sizing together with a HOLD action when evidence is mixed or thin.
- For a BUY, set a realistic stop_loss_price (below a sensible technical level) and a \
take_profit_price consistent with the horizon; entry_price should reflect the current \
close. estimated_profit_pct is your expected move to the take-profit over the horizon.
- Consider the previous recommendation for CONSISTENCY: do not whipsaw between BUY and \
SELL on marginal evidence; if you reverse a recent stance, justify it in the rationale.
- allocation_pct is the percentage of the TOTAL budget to allocate (0 for HOLD/WAIT). \
Keep it modest; downstream deterministic risk policy may reduce it further.

When "system_portfolio" is present in the payload, it is the CURRENT STATE of this \
desk's own simulated book on OTHER symbols: n_open positions, gross_exposure_pct of \
the budget already committed, and the largest positions with their weight, days held \
and unrealized P&L. Use it ONLY for portfolio construction — diversification, \
concentration and remaining room. Concretely: if gross_exposure_pct is already high, \
or the candidate would duplicate an existing large position's exposure, prefer a \
smaller allocation_pct or HOLD, and say so in the rationale. Two hard limits: an open \
position's unrealized P&L must NEVER bias this decision (no sunk-cost reasoning — \
judge THIS symbol on its own forward evidence), and the book's past outcomes are not \
in this payload, so never claim the desk has been right or wrong lately.

Never invent data that no analyst reported. Base every claim on the reports and \
context provided.

Writing rationale_it (this is the text the user actually reads):
- Write it like a good, honest consultant explaining the decision to a friend who \
knows nothing about finance: clear, concrete, and warm, never a wall of jargon.
- Say plainly WHAT to do (buy, hold, or sell — and how, e.g. gradually with DCA or \
all at once), then explain WHY, citing the analysts' concrete findings (technical, \
fundamentals, macro, corporate news) and explaining every financial term the first \
time you use it, following the ITALIAN OUTPUT STYLE block.
- Spell out the concrete RISKS the user is taking, in plain words.
- Explain what the stop_loss_price and take_profit_price mean IN PRACTICE: the stop \
loss is the price at which the position is closed to cap the loss, the take profit is \
the price at which the gain is cashed in — say roughly what each implies for the \
user's money.
- Use 6 to 10 short sentences and finish with the practical takeaway ("cosa significa \
in pratica").

Writing advice_new_investor_it and advice_holder_it (two SHORT, practical notes, in \
addition to rationale_it):
- advice_new_investor_it speaks to someone who does NOT own the stock yet: say clearly \
whether to enter now, wait for a better level (state which price or condition), or stay \
away — and why. 2 to 4 short sentences.
- advice_holder_it speaks to someone who ALREADY owns the shares: say clearly whether to \
keep, sell, take partial profits, or where to place the stop loss (state the level) — and \
why. 2 to 4 short sentences. When "user_position" is present in the payload, it is the \
user's REAL recorded (fictitious, paper-trading) position on this symbol: ground \
advice_holder_it in it — cite the actual estimated average cost ("avg_cost_est") and \
current estimated P&L ("pnl_est"/"pnl_pct_est"), and say whether stop_loss_price sits \
above or below that cost. Values ending in "_est" are ESTIMATES from session closes, not \
exact fills — say "stimato" when you cite one. A loss on the existing position must NEVER \
bias the action itself (no sunk-cost reasoning: judge the stock going forward, not the \
user's past entry). When "user_position" is absent, keep the current generic wording.
- BOTH must be CONSISTENT with your action, sizing_strategy, stop_loss_price and \
take_profit_price. For example: a HOLD means "non comprare ora" for the newcomer and \
"mantieni la posizione, con stop loss a X" for the holder; a BUY means "entra (così)" for \
the newcomer and, for the holder, whether to aggiungere or simply mantenere con lo stop; a \
SELL means "resta alla finestra / non comprare" for the newcomer and "vendi o riduci" for \
the holder. Never contradict the proposal.
- Follow the ITALIAN OUTPUT STYLE block (explain each financial term the first time you use \
it) and end each with what it means in practice.

Output STRICT JSON and NOTHING else — no markdown, no code fences, no text outside \
the single JSON object. It MUST match exactly this schema:
{
  "action": "BUY" | "SELL" | "HOLD",
  "sizing_strategy": "ALL_IN" | "DCA" | "PARTIAL" | "WAIT",
  "confidence": <number in [0.0, 1.0]>,
  "allocation_pct": <number in [0.0, 100.0]>,
  "horizon_days": <integer, typically 30>,
  "entry_price": <number or null>,
  "stop_loss_price": <number or null>,
  "take_profit_price": <number or null>,
  "estimated_profit_pct": <number>,
  "agent_weights": {"technical": <0..1>, "fundamentals": <0..1>, "macro_news": <0..1>, "corporate_news": <0..1>, "sentiment": <0..1>},
  "dissent": "<main disagreement between analysts, or null>",
  "rationale_it": "<6-10 sentences in ITALIAN, written like advice to a friend: what to do, why (citing the analysts' findings with every term explained on first use), the concrete risks, and what the stop loss / take profit levels mean in practice>",
  "advice_new_investor_it": "<2-4 short ITALIAN sentences for someone who does NOT own the stock yet: enter now, wait for which level, or stay away, and why — consistent with the action/sizing/levels>",
  "advice_holder_it": "<2-4 short ITALIAN sentences for someone who ALREADY owns the shares: keep, sell, take partial profits, or where to set the stop loss, and why — consistent with the action/sizing/levels>"
}
All keys are required. rationale_it, advice_new_investor_it and advice_holder_it are in \
Italian; everything else uses the literals above. Do not add keys that are not in the \
schema."""


class SynthesizerAgent(BaseAgent):
    """Combines the five analyst reports into a single conservative proposal."""

    name = "synthesizer"
    temperature = 0.25
    # Bumped from 2000 -> 2600 (rationale_it is 6-10 sentences with inline term
    # explanations) -> 3000 now that the payload also carries two extra Italian
    # notes (advice_new_investor_it / advice_holder_it), so the JSON needs room
    # to complete.
    max_tokens = 3000

    def _system_body(self) -> str:
        return _SYSTEM_BODY

    def build_user_prompt(
        self,
        *,
        analyst_outputs: dict[str, dict | None],
        price_summary: dict[str, Any],
        risk_profile: str,
        total_budget: float,
        currency: str,
        previous_recommendation: dict[str, Any] | None,
        user_position: dict[str, Any] | None = None,
        system_portfolio: dict[str, Any] | None = None,
    ) -> str:
        """Serialise the aggregated decision inputs as a compact JSON payload.

        ``user_position`` (when not None) is the user's REAL, fictitious
        (paper-trading) position on this symbol — see
        ``app.engine.positions.compact_position_for_prompt``. It is omitted
        from the payload entirely when there is no logged transaction, so a
        title the user has never "bought" costs zero extra tokens.

        ``system_portfolio`` (when not None) is the STATE of the desk's own
        simulated book across the OTHER symbols — see
        ``app.engine.sim_book.compact_book_for_prompt``. Without it the
        synthesizer had no way of knowing it was proposing an eighth correlated
        BUY. Omitted entirely when no position is open, so the common case costs
        nothing.
        """
        outputs = analyst_outputs if isinstance(analyst_outputs, dict) else {}
        reports = {key: outputs.get(key) for key in ANALYST_KEYS}
        payload: dict[str, Any] = {
            "analyst_reports": reports,
            "price_summary": price_summary,
            "budget": {
                "risk_profile": risk_profile,
                "total_budget": total_budget,
                "currency": currency,
            },
            "previous_recommendation": previous_recommendation,
        }
        if user_position is not None:
            payload["user_position"] = user_position
        if system_portfolio is not None:
            payload["system_portfolio"] = system_portfolio
        return (
            "Synthesize the following analyst reports and context into a single "
            "proposal, and respond with the JSON object described in your "
            "instructions. A null report means that analyst failed for this run.\n"
            + compact_json(payload)
        )

    async def run(  # type: ignore[override]
        self,
        *,
        analyst_outputs: dict[str, dict | None],
        price_summary: dict[str, Any],
        risk_profile: str,
        total_budget: float,
        currency: str,
        previous_recommendation: dict[str, Any] | None,
        lessons: list[str],
        llm: LLMClient,
        user_position: dict[str, Any] | None = None,
        system_portfolio: dict[str, Any] | None = None,
    ) -> AgentResult:
        """Run the synthesizer and return its validated proposal.

        LLM-layer exceptions propagate to the orchestrator (which treats a failed
        synthesizer as a failed run).
        """
        system = self.build_system_prompt(lessons)
        user = self.build_user_prompt(
            analyst_outputs=analyst_outputs,
            price_summary=price_summary,
            risk_profile=risk_profile,
            total_budget=total_budget,
            currency=currency,
            previous_recommendation=previous_recommendation,
            user_position=user_position,
            system_portfolio=system_portfolio,
        )
        pref_provider, pref_model = resolve_llm_pref(self.name)
        parsed, provider = await llm.complete_json(
            system,
            user,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            provider=pref_provider,
            model=pref_model,
        )
        output = self.validate_output(parsed)
        return AgentResult(agent_name=self.name, output=output, provider=provider)

    def validate_output(self, data: dict) -> dict:
        """Coerce the synthesizer proposal into its schema (blueprint 5.4).

        Enforces action/sizing enums with HOLD/WAIT fallbacks, clamps confidence
        to [0, 1] and allocation_pct to [0, 100], and normalises prices, weights,
        dissent and the Italian rationale.
        """
        raw = data if isinstance(data, dict) else {}
        return {
            "action": coerce_enum(raw.get("action"), ACTION_VALUES, Action.HOLD.value),
            "sizing_strategy": coerce_enum(
                raw.get("sizing_strategy"), SIZING_VALUES, Sizing.WAIT.value
            ),
            "confidence": clamp(coerce_float(raw.get("confidence"), 0.0), 0.0, 1.0),
            "allocation_pct": clamp(coerce_float(raw.get("allocation_pct"), 0.0), 0.0, 100.0),
            "horizon_days": coerce_int(raw.get("horizon_days"), 30, 1, 3650),
            "entry_price": coerce_optional_float(raw.get("entry_price")),
            "stop_loss_price": coerce_optional_float(raw.get("stop_loss_price")),
            "take_profit_price": coerce_optional_float(raw.get("take_profit_price")),
            "estimated_profit_pct": coerce_float(raw.get("estimated_profit_pct"), 0.0),
            "agent_weights": _coerce_weights(raw.get("agent_weights")),
            "dissent": coerce_optional_str(raw.get("dissent")),
            "rationale_it": coerce_str(raw.get("rationale_it"), ""),
            "advice_new_investor_it": coerce_str(raw.get("advice_new_investor_it"), ""),
            "advice_holder_it": coerce_str(raw.get("advice_holder_it"), ""),
        }


def _coerce_weights(value: Any) -> dict[str, float]:
    """Coerce ``agent_weights`` into the five canonical keys, each clamped to [0, 1]."""
    raw = value if isinstance(value, dict) else {}
    return {
        key: clamp(coerce_float(raw.get(key), _DEFAULT_WEIGHTS[key]), 0.0, 1.0)
        for key in ANALYST_KEYS
    }
