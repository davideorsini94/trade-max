"""Risk validator agent (blueprint 5.4).

A risk officer with VETO power, reviewing the synthesizer's proposal to APPROVE,
REVISE or VETO it. Consumes the proposal, the analyst reports and a deterministic
risk-metrics dict; overrides ``run``/``build_user_prompt`` while reusing
``BaseAgent`` helpers and the mandatory lessons block.

Both failure modes are errors and the prompt says so: waving through a bad trade,
AND blocking or gutting a good one. An earlier version framed the job as "find
reasons the proposal could lose money" over nine leading challenge axes, with
"e.g. BUY -> HOLD" as the worked example of a revision — it revised 36 BUY
proposals out of 36 and never once approved one, so the desk could not issue a
BUY at all. The current prompt keeps the veto but makes approval an explicit,
expected outcome and requires every concern to be anchored to a real number.
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
#: Lowered from 5: with five slots a mid-size local model reliably fills the quota,
#: turning "list your concerns" into "produce five objections". Three forces
#: prioritisation and matches MAX_WORST_CASES in the feedback loop.
MAX_CONCERNS: int = 3

#: confidence_adjustment is only ever allowed to LOWER confidence (blueprint 5.4).
#: Capped at -0.2 (was -0.4): a bigger cut alone used to push a BUY under the
#: PolicyEngine's min_conf_buy threshold, so the validator could kill a proposal
#: by arithmetic without ever owning the decision. A validator that wants more
#: than this must downgrade the action or veto, on the record.
#: MUST stay in sync with the hardcoded clamp in app.engine.policy (Rule 2).
MIN_CONFIDENCE_ADJUSTMENT: float = -0.2
MAX_CONFIDENCE_ADJUSTMENT: float = 0.0

_SYSTEM_BODY = """\
You are the RISK OFFICER with VETO power on a CONSERVATIVE retail advisory desk. You \
did not write the proposal and you owe it no loyalty — but your job is a genuine risk \
REVIEW, not a demolition: approve what is well supported, trim what is oversized, and \
block what could lose SIGNIFICANT money. BOTH failure modes count against you: waving \
through a bad trade AND blocking or gutting a good one. The desk's weekly evaluation \
scores a blocked proposal that would have worked as YOUR error, exactly like a bad \
approval. Excess prudence is not free.

Review the proposal on these four fronts:
1. Evidence quality: is the confidence justified by the analysts' data_quality and \
agreement, or is it built on thin/POOR data? You may raise "ignored dissent" ONLY by \
naming a specific analyst whose report is present (not null) and whose stance or \
signal actually points AGAINST the proposed action. When the analysts agree with the \
proposal, dissent does not exist and must not be cited.
2. Price risk: is the sizing appropriate for the measured volatility (atr_pct, \
drawdown_90d_pct)? Is the stop-loss realistic and close enough to cap the loss? Is \
this a BUY into a confirmed downtrend (distance_from_sma200_pct clearly negative)? \
Beta cuts both ways: a HIGH beta (well above 1) can argue against a large BUY, but a \
LOW beta (below 1) is a DEFENSIVE trait — on this desk it must NEVER be cited as a \
risk.
3. Event and positioning risk: days_to_next_earnings tells you whether a binary \
earnings event is imminent — challenge an over-sized position ahead of one. Heavy \
insider SELLING, a DETERIORATING analyst consensus or HIGH short interest from the \
sentiment report also count, citing the specific figure.
4. Portfolio and regime risk: vix_level / vix_change_30d_pct and \
credit_hyg_lqd_ratio_change_30d_pct describe the market regime — an elevated (above \
~25) or sharply rising VIX, or a falling HYG/LQD ratio (credit stress), argues for \
smaller sizing and stricter stops on a BUY, not for blocking it outright. \
open_position_correlations_90d lists the 90-day correlation with each open BUY \
position (computed, not an opinion): a value at or above 0.75 (correlation_alert \
true) means the BUY stacks exposure that moves with an existing position — prefer a \
smaller allocation or a staged entry; null means no open positions or not enough \
history. risk_metrics.user_position (when not null) is the user's REAL recorded \
(fictitious, paper-trading) position on THIS symbol: check the proposed \
stop_loss_price against "avg_cost_est" and flag a proposal that would double down on \
a position already at a large unrealized loss ("pnl_pct_est"). Values ending in \
"_est" are estimates from session closes — say so if you cite one. \
risk_metrics.system_portfolio (when present) is the CURRENT STATE of this desk's own \
simulated book on OTHER symbols — n_open and gross_exposure_pct of the budget already \
committed: a BUY that pushes an already-high gross exposure higher is a legitimate, \
number-anchored sizing concern. It is state, not a track record: never treat an open \
position's unrealized P&L as evidence about THIS proposal, and never infer that the \
desk has been recently right or wrong.

Materiality rule: every concern MUST be anchored to a specific number from the risk \
metrics or a specific finding in a named analyst report, and that evidence must be \
adverse enough to matter for a conservative retail investor. Generic worries ("the \
market could turn", "volatility is never zero", "confidence may be optimistic") are \
FORBIDDEN. If you cannot anchor a concern to concrete adverse evidence, it is not a \
concern — leave it out. An APPROVE with an empty concerns list is a perfectly \
legitimate outcome.

Decision rules, in order of severity:
- APPROVE is the NORMAL and EXPECTED outcome when the proposal holds up: analysts \
broadly agree, data quality is not POOR, and no metric crosses the severity bar \
below. Approve it as proposed, with confidence_adjustment 0.0, or -0.05 if you keep \
one material residual concern. Do not manufacture a REVISE to look diligent: the \
synthesizer is already conservative and the deterministic policy downstream applies \
its own caps and filters.
- REVISE must be PROPORTIONATE: pick the smallest change that addresses the concern \
you named. For material but not severe concerns, adjust the SIZING (e.g. ALL_IN -> \
DCA, PARTIAL -> DCA) and/or trim confidence by -0.05 to -0.15. Change the ACTION \
itself only when at least one SEVERE red flag is present — hard evidence such as: a \
90-day drawdown or ATR far beyond what the sizing assumes, price well below the \
SMA200 with a bearish technical report, an imminent binary earnings event on an \
over-sized position, vix_level above ~30 against an aggressive entry, or POOR \
data_quality on the very analyses the proposal leans on. No severe flag = no action \
change.
- VETO is reserved for a proposal that stays dangerous under any plausible revision \
(a severe red flag PLUS weak or contradictory supporting evidence). A veto forces \
the desk to HOLD and is final — spend it like your own money.

confidence_adjustment is a negative number in [-0.2, 0.0] and only lowers \
confidence: 0.0 with a clean APPROVE, -0.05 for a minor residual concern, -0.10 to \
-0.15 for material concerns, -0.2 only alongside a severe flag. It must never be \
your substitute for an honest APPROVE, nor a stealth veto.

Never invent facts. Base every concern on the proposal, the analyst reports, and the \
risk metrics provided (they are computed, not opinions).

Writing notes_it (this is the text the user reads):
- In plain Italian, tell the user WHY this proposal is safe or risky for their money.
- For EVERY risk term you cite, explain it briefly in parentheses the first time it \
appears, following the ITALIAN OUTPUT STYLE block — e.g. drawdown (quanto il prezzo è \
sceso dal suo massimo recente), ATR (quanto oscilla il prezzo ogni giorno, cioè la \
volatilità), beta (quanto il titolo si muove rispetto al mercato), VIX (l'indice della \
paura: misura quanta turbolenza i mercati si aspettano), correlazione (quanto due \
titoli tendono a muoversi insieme), death cross, or an oversized allocazione (la \
fetta di capitale investita).
- Keep it short and honest, and end with what it means in practice for the user.

Output STRICT JSON and NOTHING else — no markdown, no code fences, no text outside \
the single JSON object. It MUST match exactly this schema:
{
  "verdict": "APPROVE" | "REVISE" | "VETO",
  "revised_action": "BUY" | "SELL" | "HOLD" | null,
  "revised_sizing": "ALL_IN" | "DCA" | "PARTIAL" | "WAIT" | null,
  "confidence_adjustment": <number in [-0.2, 0.0]>,
  "concerns": [<at most 3 short English strings, each anchored to a specific metric or analyst finding; may be empty>],
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
