"""Deterministic post-LLM risk policy engine (blueprint section 6).

Pure code — no LLM, no I/O. :func:`apply` takes the synthesizer proposal, the
validator verdict and precomputed :class:`MarketMetrics`, then runs the thirteen
ordered risk rules of blueprint section 6 to produce the final conservative
recommendation (:class:`FinalReco`) plus an auditable ``list[PolicyCheck]`` —
one entry per rule, whether it fired or not, so the UI can show them all.

Design notes
------------
* Every rule appends exactly one :class:`app.schemas.PolicyCheck`; ``passed`` is
  ``True`` when the proposal already complied (rule did not intervene) and
  ``False`` when the rule modified/blocked the recommendation.
* Any modification flips ``policy_overridden`` and records the *first* (original,
  i.e. proposal) action/sizing so the UI can show "pre-policy vs final".
* ``MarketMetrics`` fields are ``float | None``: history may be too short for the
  200-day SMA, ATR, drawdown, etc. Every rule degrades gracefully to a passing
  "dati insufficienti" check when the data it needs is missing.
* All user-facing ``detail_it`` strings are Italian; code/comments are English.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.schemas import Action, PolicyCheck, Sizing, Verdict

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Number of tranches used whenever a position is forced into DCA (blueprint
#: rules 7 and 8: "DCA con dca_tranches=4, 25% a settimana").
DCA_FORCED_TRANCHES: int = 4

#: A BUY stop-loss may never sit further than this % below the entry (rule 11).
STOP_LOSS_MAX_DISTANCE_PCT: float = 8.0

#: Confidence is never allowed to reach absolute certainty (rule 12).
CONFIDENCE_CAP: float = 0.95

#: Canonical enum value-sets (single source of truth for the string literals).
_ACTION_VALUES: frozenset[str] = frozenset(a.value for a in Action)
_SIZING_VALUES: frozenset[str] = frozenset(s.value for s in Sizing)
_VERDICT_VALUES: frozenset[str] = frozenset(v.value for v in Verdict)


# --------------------------------------------------------------------------- #
# Risk profiles (blueprint section 6 table)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RiskParams:
    """Deterministic risk parameters for one risk profile (blueprint §6 table)."""

    max_position_pct: float
    cash_reserve_pct: float
    min_conf_buy: float
    min_conf_sell: float
    all_in_allowed: bool
    all_in_min_conf: float | None
    all_in_max_vol_pct: float | None
    atr_pct_force_dca: float
    max_drawdown_90d_buy: float
    cooldown_opposite_h: float


#: The three risk profiles, keyed by ``AppSettings.risk_profile`` value.
RISK_PROFILES: dict[str, RiskParams] = {
    "prudente": RiskParams(
        max_position_pct=15.0,
        cash_reserve_pct=30.0,
        min_conf_buy=0.65,
        min_conf_sell=0.60,
        all_in_allowed=False,
        all_in_min_conf=None,
        all_in_max_vol_pct=None,
        atr_pct_force_dca=3.0,
        max_drawdown_90d_buy=20.0,
        cooldown_opposite_h=72.0,
    ),
    "bilanciato": RiskParams(
        max_position_pct=20.0,
        cash_reserve_pct=20.0,
        min_conf_buy=0.60,
        min_conf_sell=0.55,
        all_in_allowed=True,
        all_in_min_conf=0.85,
        # ``MarketMetrics.volatility_30d_pct`` is ANNUALIZED (std * sqrt(252) * 100):
        # the blueprint's "vol < 2.5%" is daily-scale, i.e. ~40% annualized.
        all_in_max_vol_pct=40.0,
        atr_pct_force_dca=4.0,
        max_drawdown_90d_buy=25.0,
        cooldown_opposite_h=48.0,
    ),
    "dinamico": RiskParams(
        max_position_pct=30.0,
        cash_reserve_pct=10.0,
        min_conf_buy=0.55,
        min_conf_sell=0.50,
        all_in_allowed=True,
        all_in_min_conf=0.80,
        all_in_max_vol_pct=None,
        atr_pct_force_dca=5.0,
        max_drawdown_90d_buy=35.0,
        cooldown_opposite_h=24.0,
    ),
}

#: Fallback profile when settings carry an unknown value.
DEFAULT_PROFILE: str = "prudente"


# --------------------------------------------------------------------------- #
# Data contracts
# --------------------------------------------------------------------------- #


@dataclass
class MarketMetrics:
    """Deterministic market metrics fed to the policy engine (blueprint §6).

    Any field may be ``None`` when the underlying history is too short to
    compute it (e.g. ``sma200`` needs 200 sessions); rules handle that
    defensively.
    """

    last_close: float | None = None
    atr14: float | None = None
    sma50: float | None = None
    sma200: float | None = None
    rsi14: float | None = None
    drawdown_90d_pct: float | None = None
    volatility_30d_pct: float | None = None


@dataclass
class FinalReco:
    """The final, post-policy recommendation (blueprint §6 / recommendations table)."""

    action: str
    sizing_strategy: str
    confidence: float
    allocation_pct: float
    allocation_amount: float
    dca_tranches: int
    entry_price: float | None
    stop_loss_price: float | None
    take_profit_price: float | None
    horizon_days: int
    estimated_profit_pct: float | None
    estimated_profit_amount: float | None
    policy_overridden: bool
    original_action: str | None
    original_sizing: str | None


# --------------------------------------------------------------------------- #
# Coercion helpers (defensive: apply() accepts raw dicts, e.g. from tests)
# --------------------------------------------------------------------------- #


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _to_float(value: Any, default: float) -> float:
    """Best-effort finite float; booleans / NaN / junk fall back to ``default``."""
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _to_price(value: Any) -> float | None:
    """Positive finite float or ``None`` (prices/levels must be > 0)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or result <= 0.0:
        return None
    return result


def _to_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _norm_enum(value: Any, allowed: frozenset[str], default: str) -> str:
    if isinstance(value, str):
        candidate = value.strip().upper()
        if candidate in allowed:
            return candidate
    return default


def _norm_profile(value: Any) -> str:
    if isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in RISK_PROFILES:
            return candidate
    return DEFAULT_PROFILE


def _all_in_allowed(params: RiskParams, confidence: float, volatility: float | None) -> bool:
    """Whether the profile permits ALL_IN given the current confidence/volatility."""
    if not params.all_in_allowed:
        return False
    if params.all_in_min_conf is not None and confidence < params.all_in_min_conf:
        return False
    if params.all_in_max_vol_pct is not None:
        # Cannot confirm low volatility -> conservatively disallow.
        if volatility is None or volatility >= params.all_in_max_vol_pct:
            return False
    return True


# --------------------------------------------------------------------------- #
# The policy engine
# --------------------------------------------------------------------------- #


def apply(
    proposal: dict,
    verdict: dict,
    metrics: MarketMetrics,
    settings: Any,
    last_reco: Any,
    open_allocation_pct: float,
) -> tuple[FinalReco, list[PolicyCheck]]:
    """Apply the thirteen ordered risk rules (blueprint §6).

    Parameters
    ----------
    proposal:
        The synthesizer proposal dict (action, sizing_strategy, confidence,
        allocation_pct, entry/stop/take prices, horizon_days,
        estimated_profit_pct, ...).
    verdict:
        The validator verdict dict (verdict, revised_action, revised_sizing,
        confidence_adjustment).
    metrics:
        Precomputed :class:`MarketMetrics` (any field may be ``None``).
    settings:
        The ``AppSettings`` ORM object (reads ``risk_profile``,
        ``max_position_pct``, ``cash_reserve_pct``, ``total_budget``).
    last_reco:
        The symbol's previous ``Recommendation`` ORM object (``action`` /
        ``created_at``) or ``None``.
    open_allocation_pct:
        Sum of the still-open BUY allocations across the *other* active symbols.

    Returns
    -------
    tuple[FinalReco, list[PolicyCheck]]
        The final recommendation and the ordered list of 13 policy checks.
    """
    proposal = proposal if isinstance(proposal, dict) else {}
    verdict = verdict if isinstance(verdict, dict) else {}

    profile_key = _norm_profile(getattr(settings, "risk_profile", DEFAULT_PROFILE))
    params = RISK_PROFILES[profile_key]

    settings_max_position_pct = _to_float(getattr(settings, "max_position_pct", 100.0), 100.0)
    settings_cash_reserve_pct = _to_float(getattr(settings, "cash_reserve_pct", 0.0), 0.0)
    total_budget = _to_float(getattr(settings, "total_budget", 0.0), 0.0)
    open_alloc = max(0.0, _to_float(open_allocation_pct, 0.0))

    # --- initial (pre-policy) working state, from the proposal --------------- #
    initial_action = _norm_enum(proposal.get("action"), _ACTION_VALUES, Action.HOLD.value)
    initial_sizing = _norm_enum(proposal.get("sizing_strategy"), _SIZING_VALUES, Sizing.WAIT.value)

    action = initial_action
    sizing = initial_sizing
    confidence = _clamp(_to_float(proposal.get("confidence"), 0.0), 0.0, 1.0)
    allocation_pct = _clamp(_to_float(proposal.get("allocation_pct"), 0.0), 0.0, 100.0)
    dca_tranches = 1
    take_profit_price = _to_price(proposal.get("take_profit_price"))
    stop_loss_price = _to_price(proposal.get("stop_loss_price"))
    horizon_days = _to_int(proposal.get("horizon_days"), 30)
    estimated_profit_pct: float | None = _to_float(proposal.get("estimated_profit_pct"), 0.0)

    # entry basis: the actual advisory entry is the latest close (metrics), with
    # the synthesizer's suggested entry only as a fallback. Stop-loss (rule 11)
    # and the persisted entry_price use the same value, keeping them consistent.
    entry_basis = metrics.last_close if metrics.last_close is not None else _to_price(
        proposal.get("entry_price")
    )

    checks: list[PolicyCheck] = []
    # Mutable override tracker: first override records the proposal's action/sizing.
    override_state = {"overridden": False, "orig_action": None, "orig_sizing": None}

    def mark_override() -> None:
        if not override_state["overridden"]:
            override_state["overridden"] = True
            override_state["orig_action"] = initial_action
            override_state["orig_sizing"] = initial_sizing

    def to_hold_wait() -> None:
        nonlocal action, sizing
        action = Action.HOLD.value
        sizing = Sizing.WAIT.value

    def add(rule: str, passed: bool, detail_it: str) -> None:
        checks.append(PolicyCheck(rule=rule, passed=passed, detail_it=detail_it))

    # --- Rule 1: validator_veto -------------------------------------------- #
    verdict_value = _norm_enum(verdict.get("verdict"), _VERDICT_VALUES, Verdict.APPROVE.value)
    if verdict_value == Verdict.VETO.value:
        if action != Action.HOLD.value or sizing != Sizing.WAIT.value or allocation_pct != 0.0:
            mark_override()
        to_hold_wait()
        allocation_pct = 0.0
        add(
            "validator_veto",
            False,
            "Il validatore ha posto il VETO: proposta bloccata, azione forzata a HOLD "
            "senza alcuna allocazione. Il veto è inappellabile.",
        )
    else:
        add("validator_veto", True, "Nessun veto del validatore: la proposta può proseguire.")

    # --- Rule 2: validator_revise ------------------------------------------ #
    if verdict_value == Verdict.REVISE.value:
        changes: list[str] = []
        revised_action = _norm_enum(verdict.get("revised_action"), _ACTION_VALUES, "")
        revised_sizing = _norm_enum(verdict.get("revised_sizing"), _SIZING_VALUES, "")
        if revised_action and revised_action != action:
            changes.append(f"azione {action}→{revised_action}")
            action = revised_action
        if revised_sizing and revised_sizing != sizing:
            changes.append(f"sizing {sizing}→{revised_sizing}")
            sizing = revised_sizing
        # Keep this floor in sync with app.agents.validator.MIN_CONFIDENCE_ADJUSTMENT
        # (duplicated on purpose: the policy engine must not trust the agent layer
        # to have clamped). Widened past -0.2 and a single confidence cut can drop a
        # BUY under min_conf_buy on its own — see Rule 3 — letting the validator
        # kill a proposal by arithmetic instead of by an explicit action downgrade.
        adjustment = _clamp(_to_float(verdict.get("confidence_adjustment"), 0.0), -0.2, 0.0)
        if adjustment != 0.0:
            new_conf = _clamp(confidence + adjustment, 0.0, 1.0)
            changes.append(f"confidenza {confidence:.2f}→{new_conf:.2f}")
            confidence = new_conf
        if changes:
            mark_override()
            add(
                "validator_revise",
                False,
                "Il validatore ha richiesto una revisione: " + "; ".join(changes) + ".",
            )
        else:
            add(
                "validator_revise",
                True,
                "Revisione richiesta dal validatore senza modifiche applicabili.",
            )
    else:
        add("validator_revise", True, "Nessuna revisione richiesta dal validatore.")

    # --- Rule 3: min_confidence -------------------------------------------- #
    if action == Action.BUY.value:
        threshold = params.min_conf_buy
        if confidence < threshold:
            mark_override()
            to_hold_wait()
            add(
                "min_confidence",
                False,
                f"Confidenza {confidence:.2f} < soglia acquisto {threshold:.2f}: "
                "azione declassata a HOLD.",
            )
        else:
            add(
                "min_confidence",
                True,
                f"Confidenza {confidence:.2f} ≥ soglia acquisto {threshold:.2f}: requisito soddisfatto.",
            )
    elif action == Action.SELL.value:
        threshold = params.min_conf_sell
        if confidence < threshold:
            mark_override()
            to_hold_wait()
            add(
                "min_confidence",
                False,
                f"Confidenza {confidence:.2f} < soglia vendita {threshold:.2f}: "
                "azione declassata a HOLD.",
            )
        else:
            add(
                "min_confidence",
                True,
                f"Confidenza {confidence:.2f} ≥ soglia vendita {threshold:.2f}: requisito soddisfatto.",
            )
    else:
        add("min_confidence", True, "Nessun vincolo di confidenza per l'azione HOLD.")

    # --- Rule 4: trend_filter (solo BUY) ----------------------------------- #
    if action == Action.BUY.value:
        if metrics.last_close is None or metrics.sma50 is None or metrics.sma200 is None:
            add(
                "trend_filter",
                True,
                "Dati insufficienti per il filtro di trend (SMA200/SMA50 non disponibili): "
                "filtro non applicato.",
            )
        elif metrics.last_close < metrics.sma200 and metrics.sma50 < metrics.sma200:
            mark_override()
            to_hold_wait()
            add(
                "trend_filter",
                False,
                f"Trend ribassista conclamato (prezzo {metrics.last_close:.2f} < SMA200 "
                f"{metrics.sma200:.2f} e SMA50 {metrics.sma50:.2f} < SMA200): acquisto bloccato "
                "(HOLD).",
            )
        else:
            add(
                "trend_filter",
                True,
                f"Trend non ribassista (prezzo {metrics.last_close:.2f}, SMA50 {metrics.sma50:.2f}, "
                f"SMA200 {metrics.sma200:.2f}): acquisto consentito.",
            )
    else:
        add("trend_filter", True, "Filtro di trend applicabile solo agli acquisti.")

    # --- Rule 5: falling_knife (solo BUY) ---------------------------------- #
    if action == Action.BUY.value:
        if metrics.drawdown_90d_pct is None:
            add(
                "falling_knife",
                True,
                "Dati insufficienti per il controllo del drawdown a 90 giorni: filtro non applicato.",
            )
        elif metrics.drawdown_90d_pct > params.max_drawdown_90d_buy:
            mark_override()
            to_hold_wait()
            add(
                "falling_knife",
                False,
                f"Drawdown a 90 giorni {metrics.drawdown_90d_pct:.1f}% > soglia "
                f"{params.max_drawdown_90d_buy:.1f}%: acquisto su titolo in caduta bloccato (HOLD).",
            )
        else:
            add(
                "falling_knife",
                True,
                f"Drawdown a 90 giorni {metrics.drawdown_90d_pct:.1f}% ≤ soglia "
                f"{params.max_drawdown_90d_buy:.1f}%: nessun rischio di caduta rilevato.",
            )
    else:
        add("falling_knife", True, "Controllo del drawdown applicabile solo agli acquisti.")

    # --- Rule 6: cooldown -------------------------------------------------- #
    last_action = _norm_enum(getattr(last_reco, "action", None), _ACTION_VALUES, "")
    last_created = getattr(last_reco, "created_at", None)
    is_opposite = (action == Action.BUY.value and last_action == Action.SELL.value) or (
        action == Action.SELL.value and last_action == Action.BUY.value
    )
    if last_reco is not None and is_opposite and isinstance(last_created, datetime):
        hours_since = max(0.0, (datetime.utcnow() - last_created).total_seconds() / 3600.0)
        if hours_since < params.cooldown_opposite_h:
            mark_override()
            to_hold_wait()
            add(
                "cooldown",
                False,
                f"Ultima raccomandazione opposta ({last_action}) di appena {hours_since:.0f}h fa "
                f"(< {params.cooldown_opposite_h:.0f}h): inversione impulsiva evitata, azione forzata "
                "a HOLD.",
            )
        else:
            add(
                "cooldown",
                True,
                f"Ultima raccomandazione opposta ({last_action}) di {hours_since:.0f}h fa "
                f"(≥ {params.cooldown_opposite_h:.0f}h): fuori dalla finestra di cooldown.",
            )
    else:
        add(
            "cooldown",
            True,
            "Nessuna inversione recente da raffreddare (nessuna raccomandazione opposta ravvicinata).",
        )

    # --- Rule 7: volatility_sizing ----------------------------------------- #
    atr_pct = None
    if metrics.atr14 is not None and metrics.last_close:
        atr_pct = metrics.atr14 / metrics.last_close * 100.0
    if atr_pct is None:
        add(
            "volatility_sizing",
            True,
            "Dati insufficienti per valutare la volatilità (ATR non disponibile): controllo non applicato.",
        )
    elif atr_pct > params.atr_pct_force_dca and sizing in (Sizing.ALL_IN.value, Sizing.PARTIAL.value):
        mark_override()
        sizing = Sizing.DCA.value
        dca_tranches = DCA_FORCED_TRANCHES
        add(
            "volatility_sizing",
            False,
            f"Volatilità elevata (ATR {atr_pct:.1f}% del prezzo > soglia {params.atr_pct_force_dca:.1f}%): "
            f"ingresso forzato a DCA in {DCA_FORCED_TRANCHES} tranche.",
        )
    else:
        add(
            "volatility_sizing",
            True,
            f"Volatilità nella norma (ATR {atr_pct:.1f}% del prezzo ≤ soglia "
            f"{params.atr_pct_force_dca:.1f}%): sizing invariato.",
        )

    # --- Rule 8: all_in_gate ----------------------------------------------- #
    if sizing != Sizing.ALL_IN.value:
        add("all_in_gate", True, "Sizing non ALL_IN: nessun controllo richiesto.")
    elif _all_in_allowed(params, confidence, metrics.volatility_30d_pct):
        add(
            "all_in_gate",
            True,
            f"ALL_IN consentito dal profilo {profile_key} (confidenza e volatilità nei limiti).",
        )
    else:
        mark_override()
        sizing = Sizing.DCA.value
        dca_tranches = DCA_FORCED_TRANCHES
        add(
            "all_in_gate",
            False,
            f"ALL_IN non consentito dal profilo {profile_key}: declassato a DCA in "
            f"{DCA_FORCED_TRANCHES} tranche.",
        )

    # --- Rule 9: position_cap ---------------------------------------------- #
    effective_cap = min(params.max_position_pct, settings_max_position_pct)
    if allocation_pct > effective_cap:
        mark_override()
        previous_alloc = allocation_pct
        allocation_pct = effective_cap
        add(
            "position_cap",
            False,
            f"Allocazione ridotta al tetto per posizione: {previous_alloc:.1f}% → {allocation_pct:.1f}% "
            f"(min tra profilo {params.max_position_pct:.1f}% e impostazioni {settings_max_position_pct:.1f}%).",
        )
    else:
        add(
            "position_cap",
            True,
            f"Allocazione {allocation_pct:.1f}% entro il tetto per posizione ({effective_cap:.1f}%).",
        )

    # --- Rule 10: cash_reserve --------------------------------------------- #
    # The effective reserve is the more conservative of the profile minimum and
    # the user's configured reserve (mirrors position_cap's "most restrictive wins").
    effective_reserve = max(params.cash_reserve_pct, settings_cash_reserve_pct)
    reservable = max(0.0, 100.0 - effective_reserve - open_alloc)
    if action == Action.BUY.value:
        if reservable <= 0.0:
            mark_override()
            to_hold_wait()
            allocation_pct = 0.0
            add(
                "cash_reserve",
                False,
                f"Riserva di liquidità esaurita (riserva {effective_reserve:.1f}% + posizioni aperte "
                f"{open_alloc:.1f}% ≥ 100%): nessuna nuova allocazione possibile, azione forzata a HOLD.",
            )
        elif allocation_pct > reservable:
            mark_override()
            previous_alloc = allocation_pct
            allocation_pct = reservable
            add(
                "cash_reserve",
                False,
                f"Allocazione ridotta per rispettare la riserva di liquidità: {previous_alloc:.1f}% → "
                f"{allocation_pct:.1f}% (disponibile {reservable:.1f}% dopo riserva {effective_reserve:.1f}% "
                f"e posizioni aperte {open_alloc:.1f}%).",
            )
        else:
            add(
                "cash_reserve",
                True,
                f"Allocazione {allocation_pct:.1f}% compatibile con la riserva di liquidità "
                f"(disponibile {reservable:.1f}%).",
            )
    else:
        add("cash_reserve", True, "Nessuna nuova allocazione da verificare contro la riserva di liquidità.")

    # --- Rule 11: stop_loss_required (solo BUY) ---------------------------- #
    if action == Action.BUY.value:
        if entry_basis is None:
            add(
                "stop_loss_required",
                True,
                "Prezzo di ingresso non disponibile: impossibile calcolare lo stop-loss.",
            )
        else:
            distance_pct = None
            if stop_loss_price is not None:
                distance_pct = (entry_basis - stop_loss_price) / entry_basis * 100.0
            needs_fill = (
                stop_loss_price is None
                or distance_pct is None
                or distance_pct <= 0.0
                or distance_pct > STOP_LOSS_MAX_DISTANCE_PCT
            )
            if needs_fill:
                mark_override()
                floor_price = entry_basis * (1.0 - STOP_LOSS_MAX_DISTANCE_PCT / 100.0)
                if metrics.atr14 is not None:
                    stop_loss_price = max(entry_basis - 2.0 * metrics.atr14, floor_price)
                else:
                    stop_loss_price = floor_price
                add(
                    "stop_loss_required",
                    False,
                    f"Stop-loss assente o oltre l'{STOP_LOSS_MAX_DISTANCE_PCT:.0f}% dall'ingresso: "
                    f"impostato a {stop_loss_price:.2f} (max tra ingresso − 2·ATR e "
                    f"−{STOP_LOSS_MAX_DISTANCE_PCT:.0f}%).",
                )
            else:
                add(
                    "stop_loss_required",
                    True,
                    f"Stop-loss {stop_loss_price:.2f} entro l'{STOP_LOSS_MAX_DISTANCE_PCT:.0f}% "
                    f"dall'ingresso ({distance_pct:.1f}%): adeguato.",
                )
    else:
        add("stop_loss_required", True, "Stop-loss obbligatorio solo per gli acquisti.")

    # --- Rule 12: confidence_cap ------------------------------------------- #
    if confidence > CONFIDENCE_CAP:
        mark_override()
        previous_conf = confidence
        confidence = CONFIDENCE_CAP
        add(
            "confidence_cap",
            False,
            f"Confidenza limitata a {CONFIDENCE_CAP:.2f} (mai certezza assoluta): "
            f"{previous_conf:.2f} → {confidence:.2f}.",
        )
    else:
        add(
            "confidence_cap",
            True,
            f"Confidenza {confidence:.2f} entro il limite massimo ({CONFIDENCE_CAP:.2f}).",
        )

    # --- Rule 13: hold_normalization --------------------------------------- #
    if action == Action.HOLD.value:
        # Material normalization (sizing/allocation/tranches) counts as an
        # override; nulling estimated_profit is cosmetic cleanup that always
        # applies to a HOLD and does not, on its own, flag the recommendation.
        needs_norm = (
            sizing != Sizing.WAIT.value or allocation_pct != 0.0 or dca_tranches != 1
        )
        if needs_norm:
            mark_override()
        sizing = Sizing.WAIT.value
        allocation_pct = 0.0
        dca_tranches = 1
        estimated_profit_pct = None
        add(
            "hold_normalization",
            not needs_norm,
            "Azione finale HOLD: sizing normalizzato a ATTESA e allocazione azzerata."
            if needs_norm
            else "Azione finale HOLD già normalizzata (nessuna allocazione).",
        )
    else:
        add(
            "hold_normalization",
            True,
            "Azione operativa (BUY/SELL): nessuna normalizzazione HOLD necessaria.",
        )

    # --- Final derived amounts --------------------------------------------- #
    # A DCA plan with a single tranche is not gradual at all: when the
    # synthesizer proposed DCA without rules 7/8 forcing the tranche count,
    # normalize to the standard 4 weekly tranches.
    if sizing == Sizing.DCA.value and dca_tranches < 2:
        dca_tranches = DCA_FORCED_TRANCHES

    allocation_amount = total_budget * allocation_pct / 100.0
    if estimated_profit_pct is not None:
        estimated_profit_amount: float | None = allocation_amount * estimated_profit_pct / 100.0
    else:
        estimated_profit_amount = None

    final = FinalReco(
        action=action,
        sizing_strategy=sizing,
        confidence=confidence,
        allocation_pct=allocation_pct,
        allocation_amount=allocation_amount,
        dca_tranches=dca_tranches,
        entry_price=entry_basis,
        stop_loss_price=stop_loss_price,
        take_profit_price=take_profit_price,
        horizon_days=horizon_days,
        estimated_profit_pct=estimated_profit_pct,
        estimated_profit_amount=estimated_profit_amount,
        policy_overridden=override_state["overridden"],
        original_action=override_state["orig_action"],
        original_sizing=override_state["orig_sizing"],
    )
    return final, checks


class PolicyEngine:
    """Namespace wrapper so the orchestrator can call ``PolicyEngine.apply(...)``.

    The engine is stateless; :func:`apply` is exposed both as a module-level
    function (used by ``tests/test_policy.py``) and as this static method (used
    by ``app.engine.orchestrator``).
    """

    apply = staticmethod(apply)
