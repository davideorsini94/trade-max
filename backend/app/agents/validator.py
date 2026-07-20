"""Risk validator agent (blueprint 5.4).

An adversarial risk officer with VETO power. Its sole job is to find reasons the
synthesizer's proposal could lose significant money, and to APPROVE, REVISE, or
VETO it accordingly. Consumes the proposal, the four analyst reports and a
deterministic risk-metrics dict; overrides ``run``/``build_user_prompt`` while
reusing ``BaseAgent`` helpers and the mandatory lessons block.
"""

from __future__ import annotations

from typing import Any

from app.agents.base import (
    AgentResult,
    BaseAgent,
    clamp,
    coerce_enum,
    coerce_float,
    coerce_str,
    coerce_str_list,
    compact_json,
    resolve_llm_pref,
)
from app.agents.synthesizer import ACTION_VALUES, ANALYST_KEYS, SIZING_VALUES
from app.llm.client import LLMClient
from app.schemas import Verdict

VERDICT_VALUES: frozenset[str] = frozenset(v.value for v in Verdict)
MAX_CONCERNS: int = 5

#: confidence_adjustment is only ever allowed to LOWER confidence (blueprint 5.4).
MIN_CONFIDENCE_ADJUSTMENT: float = -0.4
MAX_CONFIDENCE_ADJUSTMENT: float = 0.0

_SYSTEM_BODY = """\
You are an adversarial RISK OFFICER with VETO power on a CONSERVATIVE retail advisory \
desk. You did not write the proposal and you are not here to be agreeable. Your job is \
to find concrete reasons the proposed action could lose SIGNIFICANT money for a \
conservative retail investor, and to block or trim it when the downside is not \
justified by the evidence.

Challenge the proposal on every axis:
- Overconfidence vs data quality: is the confidence justified by the analysts' \
data_quality and agreement, or is it built on thin/POOR data?
- Ignored dissent: did the synthesizer wave away a credible bearish analyst?
- Volatility vs sizing: is the position size (especially ALL_IN or large allocation) \
appropriate given ATR/price and 90-day drawdown? High volatility demands smaller, \
staged entries.
- Trend/valuation risk: is this a BUY while price sits well below its SMA200, or an \
expensive name with weak fundamentals?
- Catalyst risk: is a binary event (e.g. imminent earnings) about to hit an \
over-sized position?
- Stop-loss adequacy: is the stop realistic and close enough to cap the loss, or \
missing/too far?
- Positioning risk: is heavy insider SELLING, a DETERIORATING analyst consensus, or \
HIGH short interest working against a bullish proposal? The days_to_next_earnings \
value in the risk metrics tells you whether a binary earnings event is imminent.

Use the deterministic risk metrics provided (ATR as % of price, 90-day drawdown, \
beta, and distance from the SMA200) as hard evidence — they are computed, not \
opinions.

Decision rules:
- APPROVE only if the proposal is genuinely defensible for a conservative retail \
investor as-is.
- REVISE to make it safer: downgrade the action (e.g. BUY -> HOLD) and/or the sizing \
(e.g. ALL_IN -> DCA), and/or lower confidence via confidence_adjustment (a negative \
number in [-0.4, 0.0]). Only fill revised_action / revised_sizing when your verdict \
is REVISE.
- VETO when the downside risk is substantial or the supporting evidence is weak; a \
veto forces the desk to HOLD and is final.

Never invent facts. Base every concern on the proposal, the analyst reports, and the \
risk metrics provided.

Writing notes_it (this is the text the user reads):
- In plain Italian, tell the user WHY this proposal is safe or risky for their money.
- For EVERY risk term you cite, explain it briefly in parentheses the first time it \
appears, following the ITALIAN OUTPUT STYLE block — e.g. drawdown (quanto il prezzo è \
sceso dal suo massimo recente), ATR (quanto oscilla il prezzo ogni giorno, cioè la \
volatilità), beta (quanto il titolo si muove rispetto al mercato), death cross, or an \
oversized allocazione (la fetta di capitale investita).
- Keep it short and honest, and end with what it means in practice for the user.

Output STRICT JSON and NOTHING else — no markdown, no code fences, no text outside \
the single JSON object. It MUST match exactly this schema:
{
  "verdict": "APPROVE" | "REVISE" | "VETO",
  "revised_action": "BUY" | "SELL" | "HOLD" | null,
  "revised_sizing": "ALL_IN" | "DCA" | "PARTIAL" | "WAIT" | null,
  "confidence_adjustment": <number in [-0.4, 0.0]>,
  "concerns": [<at most 5 short English strings>],
  "notes_it": "<2-5 sentences in ITALIAN for the end user: explain in plain words WHY the proposal is safe or risky, explaining every risk term you cite on first use>"
}
All keys are required. revised_action and revised_sizing must be null unless the \
verdict is REVISE. concerns are in English; notes_it is in Italian. Do not add keys \
that are not in the schema."""


class RiskValidatorAgent(BaseAgent):
    """Adversarially reviews the synthesizer proposal and can APPROVE/REVISE/VETO."""

    name = "validator"
    temperature = 0.15
    # Bumped from 1400: notes_it now explains each cited risk term inline, so the
    # JSON payload needs a little more room to complete.
    max_tokens = 1600

    def _system_body(self) -> str:
        return _SYSTEM_BODY

    def build_user_prompt(
        self,
        *,
        proposal: dict[str, Any],
        analyst_outputs: dict[str, dict | None],
        risk_metrics: dict[str, Any],
    ) -> str:
        """Serialise the proposal, analyst reports and risk metrics as JSON."""
        outputs = analyst_outputs if isinstance(analyst_outputs, dict) else {}
        reports = {key: outputs.get(key) for key in ANALYST_KEYS}
        payload: dict[str, Any] = {
            "proposal": proposal,
            "analyst_reports": reports,
            "risk_metrics": risk_metrics,
        }
        return (
            "Adversarially review the following proposal against the analyst reports "
            "and deterministic risk metrics, and respond with the JSON object "
            "described in your instructions.\n" + compact_json(payload)
        )

    async def run(  # type: ignore[override]
        self,
        *,
        proposal: dict[str, Any],
        analyst_outputs: dict[str, dict | None],
        risk_metrics: dict[str, Any],
        lessons: list[str],
        llm: LLMClient,
    ) -> AgentResult:
        """Run the validator and return its validated verdict.

        LLM-layer exceptions propagate to the orchestrator.
        """
        system = self.build_system_prompt(lessons)
        user = self.build_user_prompt(
            proposal=proposal,
            analyst_outputs=analyst_outputs,
            risk_metrics=risk_metrics,
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
        """Coerce the validator verdict into its schema (blueprint 5.4).

        Enforces the verdict enum with a REVISE fallback, clamps
        confidence_adjustment to [-0.4, 0.0], truncates concerns to 5, and forces
        revised_action / revised_sizing to null unless the verdict is REVISE.
        """
        raw = data if isinstance(data, dict) else {}
        verdict = coerce_enum(raw.get("verdict"), VERDICT_VALUES, Verdict.REVISE.value)

        if verdict == Verdict.REVISE.value:
            revised_action = coerce_enum(raw.get("revised_action"), ACTION_VALUES, "") or None
            revised_sizing = coerce_enum(raw.get("revised_sizing"), SIZING_VALUES, "") or None
        else:
            # revised_* are only meaningful for a REVISE verdict.
            revised_action = None
            revised_sizing = None

        return {
            "verdict": verdict,
            "revised_action": revised_action,
            "revised_sizing": revised_sizing,
            "confidence_adjustment": clamp(
                coerce_float(raw.get("confidence_adjustment"), 0.0),
                MIN_CONFIDENCE_ADJUSTMENT,
                MAX_CONFIDENCE_ADJUSTMENT,
            ),
            "concerns": coerce_str_list(raw.get("concerns"), MAX_CONCERNS),
            "notes_it": coerce_str(raw.get("notes_it"), ""),
        }
