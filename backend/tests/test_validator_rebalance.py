"""Tests for the validator rebalance (blueprint §5.4 addendum).

Background: the validator used to REVISE 36 BUY proposals out of 36 and never
approve one, so the desk could not issue a BUY at all. Two mechanisms did it —
an explicit ``revised_action = HOLD``, and a confidence cut big enough to push
the proposal under the PolicyEngine's ``min_conf_buy`` on its own. These tests
pin the second mechanism shut (deterministic, no LLM) and pin the counterfactual
scoring that now makes a wrongful block visible in the metrics.

The prompt-behaviour half (does the model actually approve a good BUY?) cannot
be asserted deterministically — see the replay script named in the commit.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.agents.validator import (
    MAX_CONCERNS,
    MIN_CONFIDENCE_ADJUSTMENT,
    RiskValidatorAgent,
    _SYSTEM_BODY,
)
from app.engine.policy import RISK_PROFILES
from app.evaluation.evaluator import (
    CORRECT_THRESHOLD,
    HOLD_CORRECT_THRESHOLD,
    _attribute_per_agent,
    _EvalItem,
    _is_correct,
    _proposed_action,
    _validator_blocked,
)
from app.models import Analysis, AnalysisRun, Recommendation, Symbol


# --------------------------------------------------------------------------- #
# 1. The arithmetic gate: a confidence cut can no longer kill a BUY on its own
# --------------------------------------------------------------------------- #


def test_confidence_floor_cannot_breach_min_conf_buy_alone():
    """A maximum cut applied to a typical BUY confidence must stay at/above the
    policy threshold, so blocking requires an explicit action downgrade."""
    threshold = RISK_PROFILES["bilanciato"].min_conf_buy  # 0.60
    typical_buy_confidence = 0.80  # the modal value the synthesizer emits
    worst_case = typical_buy_confidence + MIN_CONFIDENCE_ADJUSTMENT
    assert worst_case >= threshold, (
        f"con floor {MIN_CONFIDENCE_ADJUSTMENT} una confidenza {typical_buy_confidence} "
        f"scende a {worst_case}, sotto la soglia {threshold}: il taglio da solo "
        "ucciderebbe ancora il BUY"
    )


def test_validator_clamps_an_oversized_confidence_cut():
    out = RiskValidatorAgent().validate_output(
        {
            "verdict": "REVISE",
            "revised_action": "HOLD",
            "confidence_adjustment": -0.9,  # model asks for far more than allowed
            "concerns": ["a", "b", "c", "d", "e", "f"],
            "notes_it": "x",
        }
    )
    assert out["confidence_adjustment"] == pytest.approx(MIN_CONFIDENCE_ADJUSTMENT)
    assert len(out["concerns"]) <= MAX_CONCERNS


def test_policy_clamp_matches_the_agent_constant():
    """The floor is duplicated in the policy engine on purpose (it must not trust
    the agent layer); this fails if the two drift apart."""
    from app.engine import policy as policy_module
    import inspect

    source = inspect.getsource(policy_module.apply)
    expected = f"{MIN_CONFIDENCE_ADJUSTMENT}, 0.0)"
    assert expected in source, (
        "il clamp in policy.apply non corrisponde a MIN_CONFIDENCE_ADJUSTMENT "
        f"({MIN_CONFIDENCE_ADJUSTMENT})"
    )


# --------------------------------------------------------------------------- #
# 2. Prompt guardrails (contract-level, not prose-level)
# --------------------------------------------------------------------------- #


def test_prompt_no_longer_suggests_the_downgrade_it_used_to_always_make():
    assert "BUY -> HOLD" not in _SYSTEM_BODY


def test_prompt_declares_approve_a_normal_outcome_and_states_the_cost_of_blocking():
    assert "NORMAL and EXPECTED" in _SYSTEM_BODY
    assert "Excess prudence is not free" in _SYSTEM_BODY


def test_prompt_states_the_confidence_range_matching_the_constant():
    assert f"[{MIN_CONFIDENCE_ADJUSTMENT}, 0.0]" in _SYSTEM_BODY


def test_prompt_forbids_citing_low_beta_as_a_risk():
    assert "NEVER be cited as a" in _SYSTEM_BODY


# --------------------------------------------------------------------------- #
# 3. Attribution helpers
# --------------------------------------------------------------------------- #


def _rec(**kwargs) -> Recommendation:
    base = dict(
        run_id=1,
        symbol_id=1,
        action="HOLD",
        sizing_strategy="WAIT",
        confidence=0.5,
        validator_verdict="REVISE",
        synthesizer_json=json.dumps({"action": "BUY"}),
        policy_checks_json="[]",
    )
    base.update(kwargs)
    return Recommendation(**base)


def _validator_analysis(**out) -> Analysis:
    return Analysis(
        run_id=1, symbol_id=1, agent_name="validator", status="OK",
        output_json=json.dumps(out),
    )


def test_proposed_action_reads_the_synthesizer_not_original_action():
    """``original_action`` is set by ANY policy override, so it cannot attribute
    the downgrade; the synthesizer's own proposal is the ground truth."""
    rec = _rec(synthesizer_json=json.dumps({"action": "BUY"}), original_action="SELL")
    assert _proposed_action(rec) == "BUY"


def test_proposed_action_falls_back_when_synthesizer_json_is_unusable():
    rec = _rec(synthesizer_json="not json", original_action="SELL")
    assert _proposed_action(rec) == "SELL"
    rec2 = _rec(synthesizer_json="{}", original_action=None, action="HOLD")
    assert _proposed_action(rec2) == "HOLD"


def test_validator_blocked_detects_an_explicit_action_downgrade():
    rec = _rec()
    assert _validator_blocked(rec, _validator_analysis(revised_action="HOLD")) is True


def test_validator_blocked_detects_the_indirect_confidence_route():
    """A cut that makes min_confidence fail is still the validator blocking."""
    rec = _rec(
        policy_checks_json=json.dumps([{"rule": "min_confidence", "passed": False}])
    )
    assert _validator_blocked(rec, _validator_analysis(confidence_adjustment=-0.2)) is True


def test_validator_not_blamed_when_a_policy_rule_did_the_downgrade():
    """Same downgrade, but the validator asked for nothing: not its error."""
    rec = _rec(
        validator_verdict="APPROVE",
        policy_checks_json=json.dumps([{"rule": "trend_filter", "passed": False}]),
    )
    assert _validator_blocked(rec, _validator_analysis(confidence_adjustment=0.0)) is False


def test_veto_always_counts_as_blocked():
    rec = _rec(validator_verdict="VETO")
    assert _validator_blocked(rec, None) is True


# --------------------------------------------------------------------------- #
# 4. HOLD is no longer "correct" just because the market was quiet
# --------------------------------------------------------------------------- #


def test_hold_needs_a_really_flat_market_to_be_correct():
    from app.evaluation.evaluator import _outcome_score

    quiet = _outcome_score("HOLD", 0.5)   # +0.5% in 7 days
    middling = _outcome_score("HOLD", 1.8)  # +1.8%: used to pass at 0.2
    assert _is_correct("HOLD", quiet) is True
    assert _is_correct("HOLD", middling) is False
    # The old generic threshold would have accepted the middling one.
    assert middling > CORRECT_THRESHOLD
    assert middling < HOLD_CORRECT_THRESHOLD


def test_directional_calls_keep_the_original_bar():
    from app.evaluation.evaluator import _outcome_score

    assert _is_correct("BUY", _outcome_score("BUY", 2.0)) is True   # +0.4
    assert _is_correct("BUY", _outcome_score("BUY", 0.5)) is False  # +0.1


# --------------------------------------------------------------------------- #
# 5. Counterfactual scoring end-to-end through _attribute_per_agent
# --------------------------------------------------------------------------- #


def _seed_blocked_buy(
    db: Session, ticker: str, *, revised_action: str = "HOLD"
) -> Recommendation:
    """A BUY proposal the validator revised down to HOLD."""
    symbol = Symbol(ticker=ticker, name=f"{ticker} Inc.", exchange="NASDAQ", currency="USD")
    db.add(symbol)
    db.commit()
    db.refresh(symbol)

    run = AnalysisRun(symbol_id=symbol.id, status="COMPLETED", trigger="MANUAL")
    db.add(run)
    db.flush()
    db.add(
        Analysis(
            run_id=run.id, symbol_id=symbol.id, agent_name="validator", status="OK",
            confidence=0.6,
            output_json=json.dumps(
                {"verdict": "REVISE", "revised_action": revised_action,
                 "confidence_adjustment": -0.15}
            ),
        )
    )
    rec = Recommendation(
        run_id=run.id,
        symbol_id=symbol.id,
        action=revised_action,
        sizing_strategy="WAIT",
        confidence=0.6,
        entry_price=100.0,
        validator_verdict="REVISE",
        synthesizer_json=json.dumps({"action": "BUY", "confidence": 0.8}),
        policy_checks_json="[]",
        created_at=datetime.utcnow() - timedelta(days=8),
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


def _validator_accuracy(db: Session, rec: Recommendation, ret: float) -> dict:
    score_for_final = 1.0 - min(abs(ret) / 5.0, 1.0) * 2.0  # HOLD score
    item = _EvalItem(
        rec=rec, ret=ret, score=score_for_final,
        correct=_is_correct(rec.action, score_for_final),
    )
    return _attribute_per_agent(db, [item])["validator"]


def test_blocking_a_winner_is_scored_as_a_validator_error(
    db_session_factory: sessionmaker[Session],
) -> None:
    """THE regression this fixes: the blocked BUY would have earned +5%."""
    db = db_session_factory()
    try:
        rec = _seed_blocked_buy(db, "WINR")
        metrics = _validator_accuracy(db, rec, ret=5.0)
        assert metrics["n_samples"] == 1
        assert metrics["accuracy"] == pytest.approx(0.0)
    finally:
        db.close()


def test_blocking_a_loser_still_earns_full_credit(
    db_session_factory: sessionmaker[Session],
) -> None:
    db = db_session_factory()
    try:
        rec = _seed_blocked_buy(db, "LOSR")
        metrics = _validator_accuracy(db, rec, ret=-5.0)
        assert metrics["n_samples"] == 1
        assert metrics["accuracy"] == pytest.approx(1.0)
    finally:
        db.close()


def test_an_immaterial_block_contributes_no_sample(
    db_session_factory: sessionmaker[Session],
) -> None:
    """+0.5% would have scored inside the grey zone: neither credit nor blame,
    rather than a coin flip that muddies the metric."""
    db = db_session_factory()
    try:
        rec = _seed_blocked_buy(db, "GREY")
        metrics = _validator_accuracy(db, rec, ret=0.5)
        assert metrics["n_samples"] == 0
    finally:
        db.close()
