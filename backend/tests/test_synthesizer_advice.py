"""Tests for the synthesizer's two audience-specific advice notes.

Covers both ends of the contract:

* ``SynthesizerAgent.validate_output`` keeps ``advice_new_investor_it`` /
  ``advice_holder_it`` when present (trimmed via the shared string helper) and
  defaults them to ``""`` when the model omits them.
* ``reco_to_out`` (exercised through ``GET .../recommendations/latest``) surfaces
  both notes from the persisted ``synthesizer_json``, and degrades to ``None``
  when they are empty or absent (older recommendations predating the fields).
"""

from __future__ import annotations

import json
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.agents.synthesizer import SynthesizerAgent
from app.models import AnalysisRun, Recommendation, Symbol


def test_validate_output_keeps_new_advice_fields() -> None:
    out = SynthesizerAgent().validate_output(
        {
            "action": "HOLD",
            "sizing_strategy": "WAIT",
            "confidence": 0.5,
            "advice_new_investor_it": "  Non comprare ora: aspetta un ritracciamento.  ",
            "advice_holder_it": "Mantieni la posizione, con stop loss a 90.",
        }
    )
    assert out["advice_new_investor_it"] == "Non comprare ora: aspetta un ritracciamento."
    assert out["advice_holder_it"] == "Mantieni la posizione, con stop loss a 90."


def test_validate_output_defaults_missing_advice_to_empty_string() -> None:
    out = SynthesizerAgent().validate_output({"action": "BUY"})
    assert out["advice_new_investor_it"] == ""
    assert out["advice_holder_it"] == ""


def _seed_reco(factory: sessionmaker[Session], synthesizer_json: str) -> int:
    """Insert a symbol + a COMPLETED run + one recommendation; return the symbol id."""
    session = factory()
    try:
        symbol = Symbol(ticker="ADV1", name="Advice Inc.", exchange="NASDAQ", currency="USD")
        session.add(symbol)
        session.commit()
        session.refresh(symbol)
        symbol_id = symbol.id

        now = datetime.utcnow()
        run = AnalysisRun(
            symbol_id=symbol_id,
            status="COMPLETED",
            trigger="MANUAL",
            started_at=now,
            finished_at=now,
        )
        session.add(run)
        session.commit()
        session.refresh(run)

        session.add(
            Recommendation(
                run_id=run.id,
                symbol_id=symbol_id,
                action="HOLD",
                sizing_strategy="WAIT",
                confidence=0.5,
                rationale_it="Motivazione.",
                synthesizer_json=synthesizer_json,
                validator_verdict="APPROVE",
            )
        )
        session.commit()
        return symbol_id
    finally:
        session.close()


def test_reco_out_surfaces_advice_from_synthesizer_json(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    synth = json.dumps(
        {
            "action": "HOLD",
            "advice_new_investor_it": "Meglio aspettare un livello più basso prima di entrare.",
            "advice_holder_it": "Mantieni la posizione, con stop loss a 90.",
        }
    )
    symbol_id = _seed_reco(db_session_factory, synth)

    resp = client.get(f"/api/symbols/{symbol_id}/recommendations/latest")
    assert resp.status_code == 200
    body = resp.json()
    assert body["advice_new_investor_it"] == "Meglio aspettare un livello più basso prima di entrare."
    assert body["advice_holder_it"] == "Mantieni la posizione, con stop loss a 90."


def test_reco_out_advice_none_when_missing_or_empty(
    client: TestClient, db_session_factory: sessionmaker[Session]
) -> None:
    # advice_new_investor_it is whitespace-only, advice_holder_it is absent:
    # both must degrade to None rather than "" (mirrors older rows).
    synth = json.dumps({"action": "HOLD", "advice_new_investor_it": "   "})
    symbol_id = _seed_reco(db_session_factory, synth)

    resp = client.get(f"/api/symbols/{symbol_id}/recommendations/latest")
    assert resp.status_code == 200
    body = resp.json()
    assert body["advice_new_investor_it"] is None
    assert body["advice_holder_it"] is None


# --------------------------------------------------------------------------- #
# user_position wiring (blueprint §5.4 addendum, paper-trading positions)
# --------------------------------------------------------------------------- #


def _build_prompt_kwargs(**overrides):
    base = dict(
        analyst_outputs={},
        price_summary={"close": 100.0},
        risk_profile="prudente",
        total_budget=10000.0,
        currency="USD",
        previous_recommendation=None,
    )
    base.update(overrides)
    return base


def test_build_user_prompt_includes_user_position_when_present() -> None:
    position = {"status": "OPEN", "ccy": "USD", "invested": 1000.0, "pnl_est": 104.0}
    prompt = SynthesizerAgent().build_user_prompt(
        **_build_prompt_kwargs(user_position=position)
    )
    payload = json.loads(prompt.split("\n", 1)[1])
    assert payload["user_position"] == position


def test_build_user_prompt_omits_user_position_key_when_none() -> None:
    prompt = SynthesizerAgent().build_user_prompt(**_build_prompt_kwargs(user_position=None))
    payload = json.loads(prompt.split("\n", 1)[1])
    assert "user_position" not in payload


def test_build_user_prompt_omits_user_position_by_default() -> None:
    # No user_position kwarg at all -> defaults to None -> key absent (zero
    # extra tokens for the vast majority of runs with no logged transaction).
    prompt = SynthesizerAgent().build_user_prompt(**_build_prompt_kwargs())
    payload = json.loads(prompt.split("\n", 1)[1])
    assert "user_position" not in payload
