"""Deterministic, offline unit tests for the risk policy engine (blueprint §6).

Every test exercises ``app.engine.policy.apply`` with lightweight
``SimpleNamespace`` stand-ins for the ``AppSettings`` / ``Recommendation`` ORM
objects — no DB, no network, no LLM. Each test isolates one of the thirteen
rules by starting from a "clean" BUY scenario that trips nothing, then changing
only the input relevant to the rule under test.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.engine.policy import (
    CONFIDENCE_CAP,
    DCA_FORCED_TRANCHES,
    RISK_PROFILES,
    FinalReco,
    MarketMetrics,
    PolicyEngine,
    apply,
)
from app.schemas import PolicyCheck

# The exact rule keys, in blueprint order.
EXPECTED_RULES = [
    "validator_veto",
    "validator_revise",
    "min_confidence",
    "trend_filter",
    "falling_knife",
    "cooldown",
    "volatility_sizing",
    "all_in_gate",
    "position_cap",
    "cash_reserve",
    "stop_loss_required",
    "confidence_cap",
    "hold_normalization",
]


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def make_settings(
    risk_profile: str = "dinamico",
    total_budget: float = 10000.0,
    max_position_pct: float = 50.0,
    cash_reserve_pct: float = 10.0,
) -> SimpleNamespace:
    return SimpleNamespace(
        risk_profile=risk_profile,
        total_budget=total_budget,
        max_position_pct=max_position_pct,
        cash_reserve_pct=cash_reserve_pct,
        budget_currency="EUR",
    )


def make_metrics(
    last_close: float | None = 100.0,
    atr14: float | None = 1.0,
    sma50: float | None = 95.0,
    sma200: float | None = 90.0,
    rsi14: float | None = 55.0,
    drawdown_90d_pct: float | None = 5.0,
    volatility_30d_pct: float | None = 1.0,
) -> MarketMetrics:
    return MarketMetrics(
        last_close=last_close,
        atr14=atr14,
        sma50=sma50,
        sma200=sma200,
        rsi14=rsi14,
        drawdown_90d_pct=drawdown_90d_pct,
        volatility_30d_pct=volatility_30d_pct,
    )


def make_proposal(
    action: str = "BUY",
    sizing_strategy: str = "PARTIAL",
    confidence: float = 0.70,
    allocation_pct: float = 10.0,
    entry_price: float | None = 100.0,
    stop_loss_price: float | None = 95.0,
    take_profit_price: float | None = 115.0,
    horizon_days: int = 30,
    estimated_profit_pct: float = 10.0,
) -> dict:
    return {
        "action": action,
        "sizing_strategy": sizing_strategy,
        "confidence": confidence,
        "allocation_pct": allocation_pct,
        "entry_price": entry_price,
        "stop_loss_price": stop_loss_price,
        "take_profit_price": take_profit_price,
        "horizon_days": horizon_days,
        "estimated_profit_pct": estimated_profit_pct,
    }


def make_verdict(
    verdict: str = "APPROVE",
    revised_action: str | None = None,
    revised_sizing: str | None = None,
    confidence_adjustment: float = 0.0,
) -> dict:
    return {
        "verdict": verdict,
        "revised_action": revised_action,
        "revised_sizing": revised_sizing,
        "confidence_adjustment": confidence_adjustment,
        "concerns": [],
        "notes_it": "",
    }


def run(
    *,
    proposal: dict | None = None,
    verdict: dict | None = None,
    metrics: MarketMetrics | None = None,
    settings: SimpleNamespace | None = None,
    last_reco: SimpleNamespace | None = None,
    open_allocation_pct: float = 0.0,
) -> tuple[FinalReco, dict[str, PolicyCheck]]:
    """Run ``apply`` and return (final, {rule -> check}) for convenient assertions."""
    final, checks = apply(
        proposal or make_proposal(),
        verdict or make_verdict(),
        metrics or make_metrics(),
        settings or make_settings(),
        last_reco,
        open_allocation_pct,
    )
    by_rule = {c.rule: c for c in checks}
    return final, by_rule


# --------------------------------------------------------------------------- #
# Structural guarantees
# --------------------------------------------------------------------------- #


def test_risk_profiles_table_values() -> None:
    prudente = RISK_PROFILES["prudente"]
    assert prudente.max_position_pct == 15.0
    assert prudente.cash_reserve_pct == 30.0
    assert prudente.min_conf_buy == 0.65
    assert prudente.min_conf_sell == 0.60
    assert prudente.all_in_allowed is False
    assert prudente.atr_pct_force_dca == 3.0
    assert prudente.max_drawdown_90d_buy == 20.0
    assert prudente.cooldown_opposite_h == 72.0

    bilanciato = RISK_PROFILES["bilanciato"]
    # Threshold on the ANNUALIZED volatility scale: blueprint's "vol < 2.5%"
    # is daily-scale, i.e. 2.5 * sqrt(252) ≈ 40% annualized.
    assert (bilanciato.all_in_min_conf, bilanciato.all_in_max_vol_pct) == (0.85, 40.0)
    assert bilanciato.cooldown_opposite_h == 48.0

    dinamico = RISK_PROFILES["dinamico"]
    assert dinamico.max_position_pct == 30.0
    assert dinamico.all_in_min_conf == 0.80
    assert dinamico.all_in_max_vol_pct is None
    assert dinamico.cooldown_opposite_h == 24.0


def test_all_thirteen_checks_present_in_order() -> None:
    _, checks = apply(
        make_proposal(), make_verdict(), make_metrics(), make_settings(), None, 0.0
    )
    assert [c.rule for c in checks] == EXPECTED_RULES
    assert all(isinstance(c, PolicyCheck) for c in checks)
    assert all(c.detail_it for c in checks)  # every check carries an Italian detail


def test_clean_buy_passes_untouched() -> None:
    final, by_rule = run()
    assert final.action == "BUY"
    assert final.sizing_strategy == "PARTIAL"
    assert final.policy_overridden is False
    assert final.original_action is None
    assert final.original_sizing is None
    assert all(c.passed for c in by_rule.values())
    # allocation_amount = total_budget * allocation_pct / 100
    assert final.allocation_amount == 1000.0
    # estimated_profit_amount = allocation_amount * estimated_profit_pct / 100
    assert final.estimated_profit_amount == 100.0
    assert final.entry_price == 100.0


# --------------------------------------------------------------------------- #
# Rule 1: validator_veto
# --------------------------------------------------------------------------- #


def test_veto_forces_hold_wait_zero_allocation() -> None:
    final, by_rule = run(verdict=make_verdict(verdict="VETO"))
    assert final.action == "HOLD"
    assert final.sizing_strategy == "WAIT"
    assert final.allocation_pct == 0.0
    assert final.allocation_amount == 0.0
    assert final.policy_overridden is True
    assert final.original_action == "BUY"
    assert final.original_sizing == "PARTIAL"
    assert by_rule["validator_veto"].passed is False
    # estimated profit nulled by hold normalization
    assert final.estimated_profit_pct is None
    assert final.estimated_profit_amount is None


# --------------------------------------------------------------------------- #
# Rule 2: validator_revise
# --------------------------------------------------------------------------- #


def test_revise_applies_sizing_and_confidence_adjustment() -> None:
    final, by_rule = run(
        proposal=make_proposal(confidence=0.80, sizing_strategy="PARTIAL"),
        verdict=make_verdict(
            verdict="REVISE", revised_sizing="DCA", confidence_adjustment=-0.10
        ),
    )
    assert final.action == "BUY"  # unchanged (no revised_action)
    assert final.sizing_strategy == "DCA"
    assert final.confidence == pytest.approx(0.70)  # 0.80 - 0.10
    assert final.policy_overridden is True
    assert by_rule["validator_revise"].passed is False


def test_revise_applies_revised_action() -> None:
    final, by_rule = run(
        verdict=make_verdict(verdict="REVISE", revised_action="HOLD"),
    )
    assert final.action == "HOLD"
    assert by_rule["validator_revise"].passed is False
    assert final.original_action == "BUY"
    # HOLD => normalized
    assert final.sizing_strategy == "WAIT"
    assert final.allocation_pct == 0.0


def test_revise_without_changes_passes() -> None:
    final, by_rule = run(
        verdict=make_verdict(verdict="REVISE", confidence_adjustment=0.0),
    )
    assert by_rule["validator_revise"].passed is True
    assert final.action == "BUY"


# --------------------------------------------------------------------------- #
# Rule 3: min_confidence
# --------------------------------------------------------------------------- #


def test_buy_below_min_confidence_downgraded_to_hold() -> None:
    # prudente requires 0.65 for BUY; 0.60 is below.
    final, by_rule = run(
        proposal=make_proposal(confidence=0.60),
        settings=make_settings(risk_profile="prudente"),
    )
    assert final.action == "HOLD"
    assert final.sizing_strategy == "WAIT"
    assert final.allocation_pct == 0.0
    assert final.policy_overridden is True
    assert final.original_action == "BUY"
    assert by_rule["min_confidence"].passed is False


def test_sell_below_min_confidence_downgraded() -> None:
    # dinamico requires 0.50 for SELL; 0.40 is below. Use a non-opposite last_reco
    # so cooldown does not also fire.
    final, by_rule = run(
        proposal=make_proposal(action="SELL", sizing_strategy="PARTIAL", confidence=0.40),
    )
    assert final.action == "HOLD"
    assert by_rule["min_confidence"].passed is False


def test_buy_at_min_confidence_passes() -> None:
    final, by_rule = run(
        proposal=make_proposal(confidence=0.65),
        settings=make_settings(risk_profile="prudente"),
    )
    assert final.action == "BUY"
    assert by_rule["min_confidence"].passed is True


# --------------------------------------------------------------------------- #
# Rule 4: trend_filter
# --------------------------------------------------------------------------- #


def test_death_cross_blocks_buy() -> None:
    final, by_rule = run(
        proposal=make_proposal(confidence=0.90),
        metrics=make_metrics(last_close=80.0, sma50=90.0, sma200=100.0),
    )
    assert final.action == "HOLD"
    assert by_rule["trend_filter"].passed is False
    assert final.original_action == "BUY"


def test_trend_filter_insufficient_data_passes() -> None:
    final, by_rule = run(
        proposal=make_proposal(confidence=0.90),
        metrics=make_metrics(sma200=None),
    )
    assert final.action == "BUY"
    assert by_rule["trend_filter"].passed is True
    assert "insufficient" in by_rule["trend_filter"].detail_it.lower()


# --------------------------------------------------------------------------- #
# Rule 5: falling_knife
# --------------------------------------------------------------------------- #


def test_falling_knife_blocks_buy() -> None:
    # prudente max drawdown for BUY is 20%; 40% is a falling knife. Keep the
    # trend non-bearish so only falling_knife fires.
    final, by_rule = run(
        proposal=make_proposal(confidence=0.90),
        metrics=make_metrics(
            last_close=100.0, sma50=95.0, sma200=90.0, drawdown_90d_pct=40.0
        ),
        settings=make_settings(risk_profile="prudente"),
    )
    assert final.action == "HOLD"
    assert by_rule["falling_knife"].passed is False
    assert by_rule["trend_filter"].passed is True


# --------------------------------------------------------------------------- #
# Rule 6: cooldown
# --------------------------------------------------------------------------- #


def test_cooldown_blocks_opposite_action_within_window() -> None:
    # dinamico cooldown = 24h; a SELL 10h ago blocks a fresh BUY.
    last = SimpleNamespace(action="SELL", created_at=datetime.utcnow() - timedelta(hours=10))
    final, by_rule = run(proposal=make_proposal(confidence=0.90), last_reco=last)
    assert final.action == "HOLD"
    assert by_rule["cooldown"].passed is False
    assert final.original_action == "BUY"


def test_cooldown_outside_window_passes() -> None:
    last = SimpleNamespace(action="SELL", created_at=datetime.utcnow() - timedelta(hours=50))
    final, by_rule = run(proposal=make_proposal(confidence=0.90), last_reco=last)
    assert final.action == "BUY"
    assert by_rule["cooldown"].passed is True


def test_cooldown_same_direction_passes() -> None:
    last = SimpleNamespace(action="BUY", created_at=datetime.utcnow() - timedelta(hours=1))
    final, by_rule = run(proposal=make_proposal(confidence=0.90), last_reco=last)
    assert final.action == "BUY"
    assert by_rule["cooldown"].passed is True


# --------------------------------------------------------------------------- #
# Rule 7: volatility_sizing
# --------------------------------------------------------------------------- #


def test_high_atr_forces_dca_with_four_tranches() -> None:
    # atr14=10 on last_close=100 => ATR 10% > dinamico threshold 5%.
    final, by_rule = run(
        proposal=make_proposal(confidence=0.90, sizing_strategy="PARTIAL"),
        metrics=make_metrics(atr14=10.0),
    )
    assert final.sizing_strategy == "DCA"
    assert final.dca_tranches == DCA_FORCED_TRANCHES == 4
    assert by_rule["volatility_sizing"].passed is False
    assert final.action == "BUY"


def test_normal_atr_leaves_sizing_untouched() -> None:
    final, by_rule = run(proposal=make_proposal(sizing_strategy="PARTIAL"))
    assert final.sizing_strategy == "PARTIAL"
    assert by_rule["volatility_sizing"].passed is True


# --------------------------------------------------------------------------- #
# Rule 8: all_in_gate
# --------------------------------------------------------------------------- #


def test_all_in_blocked_for_prudente() -> None:
    # prudente never allows ALL_IN. Keep ATR low so volatility_sizing does not
    # convert first (its detail would otherwise mask all_in_gate).
    final, by_rule = run(
        proposal=make_proposal(confidence=0.90, sizing_strategy="ALL_IN"),
        settings=make_settings(risk_profile="prudente"),
    )
    assert final.sizing_strategy == "DCA"
    assert final.dca_tranches == 4
    assert by_rule["all_in_gate"].passed is False


def test_all_in_allowed_for_dinamico() -> None:
    final, by_rule = run(
        proposal=make_proposal(confidence=0.85, sizing_strategy="ALL_IN"),
        settings=make_settings(risk_profile="dinamico"),
    )
    assert final.sizing_strategy == "ALL_IN"
    assert by_rule["all_in_gate"].passed is True


def test_all_in_balanced_requires_low_volatility() -> None:
    # bilanciato allows ALL_IN only when conf>=0.85 AND annualized vol < 40%
    # (blueprint: "vol < 2.5%" daily ≈ 40% annualized).
    final, by_rule = run(
        proposal=make_proposal(confidence=0.90, sizing_strategy="ALL_IN"),
        metrics=make_metrics(atr14=1.0, volatility_30d_pct=55.0),
        settings=make_settings(risk_profile="bilanciato"),
    )
    assert final.sizing_strategy == "DCA"
    assert by_rule["all_in_gate"].passed is False


def test_all_in_balanced_allowed_when_calm_and_confident() -> None:
    # The documented allowance must be reachable: high confidence + genuinely
    # low annualized volatility keeps ALL_IN for the bilanciato profile.
    final, by_rule = run(
        proposal=make_proposal(confidence=0.90, sizing_strategy="ALL_IN"),
        metrics=make_metrics(atr14=1.0, volatility_30d_pct=20.0),
        settings=make_settings(risk_profile="bilanciato"),
    )
    assert final.sizing_strategy == "ALL_IN"
    assert by_rule["all_in_gate"].passed is True


# --------------------------------------------------------------------------- #
# Rule 9: position_cap
# --------------------------------------------------------------------------- #


def test_position_cap_honors_min_of_profile_and_settings() -> None:
    # dinamico profile cap = 30%, settings cap = 5% => effective 5%.
    final, by_rule = run(
        proposal=make_proposal(confidence=0.90, allocation_pct=20.0),
        settings=make_settings(risk_profile="dinamico", max_position_pct=5.0),
    )
    assert final.allocation_pct == 5.0
    assert final.allocation_amount == 500.0  # 10000 * 5%
    assert by_rule["position_cap"].passed is False
    assert final.policy_overridden is True


def test_position_cap_profile_tighter_than_settings() -> None:
    # prudente profile cap = 15%, settings cap = 50% => effective 15%.
    final, by_rule = run(
        proposal=make_proposal(confidence=0.90, allocation_pct=40.0),
        settings=make_settings(risk_profile="prudente", max_position_pct=50.0, cash_reserve_pct=10.0),
    )
    assert final.allocation_pct == 15.0
    assert by_rule["position_cap"].passed is False


# --------------------------------------------------------------------------- #
# Rule 10: cash_reserve
# --------------------------------------------------------------------------- #


def test_cash_reserve_exhaustion_forces_hold() -> None:
    # dinamico reserve = 10%; open positions 95% => reservable = 0 => HOLD.
    final, by_rule = run(
        proposal=make_proposal(confidence=0.90, allocation_pct=10.0),
        settings=make_settings(risk_profile="dinamico", max_position_pct=50.0, cash_reserve_pct=10.0),
        open_allocation_pct=95.0,
    )
    assert final.action == "HOLD"
    assert final.allocation_pct == 0.0
    assert by_rule["cash_reserve"].passed is False
    assert final.original_action == "BUY"


def test_cash_reserve_reduces_allocation() -> None:
    # reservable = 100 - 10 (reserve) - 82 (open) = 8; requested 20 -> capped to 8.
    final, by_rule = run(
        proposal=make_proposal(confidence=0.90, allocation_pct=20.0),
        settings=make_settings(risk_profile="dinamico", max_position_pct=50.0, cash_reserve_pct=10.0),
        open_allocation_pct=82.0,
    )
    assert final.action == "BUY"
    assert final.allocation_pct == 8.0
    assert by_rule["cash_reserve"].passed is False


# --------------------------------------------------------------------------- #
# Rule 11: stop_loss_required
# --------------------------------------------------------------------------- #


def test_stop_loss_autofilled_when_too_far() -> None:
    # stop 50 is 50% below entry 100 -> autofilled to max(100-2*1, 92) = 98.
    final, by_rule = run(
        proposal=make_proposal(confidence=0.90, stop_loss_price=50.0),
        metrics=make_metrics(last_close=100.0, atr14=1.0),
    )
    assert final.stop_loss_price == 98.0
    assert by_rule["stop_loss_required"].passed is False
    # never more than 8% away
    assert (final.entry_price - final.stop_loss_price) / final.entry_price <= 0.08


def test_stop_loss_autofilled_when_missing_and_no_atr() -> None:
    final, by_rule = run(
        proposal=make_proposal(confidence=0.90, stop_loss_price=None),
        metrics=make_metrics(last_close=100.0, atr14=None),
    )
    # falls back to entry * 0.92
    assert final.stop_loss_price == pytest.approx(92.0)
    assert by_rule["stop_loss_required"].passed is False


def test_stop_loss_within_range_kept() -> None:
    final, by_rule = run(
        proposal=make_proposal(confidence=0.90, stop_loss_price=95.0),
        metrics=make_metrics(last_close=100.0, atr14=1.0),
    )
    assert final.stop_loss_price == 95.0
    assert by_rule["stop_loss_required"].passed is True


# --------------------------------------------------------------------------- #
# Rule 12: confidence_cap
# --------------------------------------------------------------------------- #


def test_confidence_capped_at_0_95() -> None:
    final, by_rule = run(proposal=make_proposal(confidence=0.99))
    assert final.confidence == CONFIDENCE_CAP == 0.95
    assert by_rule["confidence_cap"].passed is False
    assert final.policy_overridden is True


def test_confidence_below_cap_untouched() -> None:
    final, by_rule = run(proposal=make_proposal(confidence=0.80))
    assert final.confidence == 0.80
    assert by_rule["confidence_cap"].passed is True


# --------------------------------------------------------------------------- #
# Rule 13: hold_normalization
# --------------------------------------------------------------------------- #


def test_hold_normalization_zeroes_allocation() -> None:
    # A HOLD proposal that (oddly) carries an allocation gets normalized.
    final, by_rule = run(
        proposal=make_proposal(
            action="HOLD", sizing_strategy="PARTIAL", allocation_pct=20.0, estimated_profit_pct=10.0
        ),
    )
    assert final.action == "HOLD"
    assert final.sizing_strategy == "WAIT"
    assert final.allocation_pct == 0.0
    assert final.allocation_amount == 0.0
    assert final.dca_tranches == 1
    assert final.estimated_profit_pct is None
    assert final.estimated_profit_amount is None
    assert by_rule["hold_normalization"].passed is False


def test_hold_already_normalized_is_not_an_override() -> None:
    # A HOLD proposal that is already WAIT / 0 allocation is left as-is: the
    # cosmetic nulling of estimated_profit does not flag it as overridden.
    final, by_rule = run(
        proposal=make_proposal(
            action="HOLD", sizing_strategy="WAIT", allocation_pct=0.0, estimated_profit_pct=0.0
        ),
    )
    assert final.action == "HOLD"
    assert final.estimated_profit_pct is None
    assert final.estimated_profit_amount is None
    assert by_rule["hold_normalization"].passed is True
    assert final.policy_overridden is False


# --------------------------------------------------------------------------- #
# PolicyEngine namespace wrapper
# --------------------------------------------------------------------------- #


def test_policy_engine_apply_matches_module_function() -> None:
    args = (make_proposal(), make_verdict(), make_metrics(), make_settings(), None, 0.0)
    final_a, checks_a = apply(*args)
    final_b, checks_b = PolicyEngine.apply(*args)
    assert final_a == final_b
    assert [c.model_dump() for c in checks_a] == [c.model_dump() for c in checks_b]
