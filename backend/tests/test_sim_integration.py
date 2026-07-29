"""Il libro simulato arriva davvero ai prompt (blueprint §6 addendum).

Il valore del libro sta nel cambiare le DECISIONI, non nel riempire una pagina:
senza questo blocco né il sintetizzatore né il validatore potevano sapere che
stavano proponendo l'ottavo acquisto correlato. Questi test verificano che il
blocco entri nel payload, che sia OMESSO quando il libro è vuoto (per non pagare
token sul caso comune) e che non porti mai statistiche di performance, che
farebbero ancorare il modello a una striscia fortunata.
"""

from __future__ import annotations

import json

from app.agents.synthesizer import SynthesizerAgent
from app.agents.validator import RiskValidatorAgent
from app.engine.sim_book import aggregate_book, compact_book_for_prompt

_BOOK = {
    "n_open": 3,
    "gross_exposure_pct": 35.0,
    "positions": [{"t": "AAA", "w_pct": 15.0, "days": 4, "pnl_pct": 2.1}],
}


def _synth_prompt(system_portfolio: dict | None) -> str:
    return SynthesizerAgent().build_user_prompt(
        analyst_outputs={},
        price_summary={"close": 100.0},
        risk_profile="bilanciato",
        total_budget=10_000.0,
        currency="USD",
        previous_recommendation=None,
        system_portfolio=system_portfolio,
    )


def test_book_state_reaches_the_synthesizer_payload() -> None:
    payload = json.loads(_synth_prompt(_BOOK).split("\n", 1)[1])
    assert payload["system_portfolio"]["gross_exposure_pct"] == 35.0
    assert payload["system_portfolio"]["n_open"] == 3


def test_empty_book_costs_zero_extra_tokens() -> None:
    """Sul caso comune di oggi (nessuna posizione) la chiave non deve esistere."""
    assert "system_portfolio" not in _synth_prompt(None)


def test_synthesizer_is_told_how_to_use_it_and_forbidden_sunk_cost() -> None:
    system = SynthesizerAgent().build_system_prompt([])
    assert "system_portfolio" in system
    assert "gross_exposure_pct" in system
    # Il presidio che conta: il P&L di una posizione aperta non deve influenzare
    # la decisione su un ALTRO titolo.
    assert "sunk-cost" in system
    assert "never claim the desk has been right or wrong lately" in system


def test_validator_is_told_it_is_state_not_a_track_record() -> None:
    system = RiskValidatorAgent().build_system_prompt([])
    assert "risk_metrics.system_portfolio" in system
    assert "state, not a track record" in system


def test_prompt_block_carries_no_performance_statistics() -> None:
    """Il blocco è costruito da ``compact_book_for_prompt``: solo stato."""
    positions = [
        {
            "status": "OPEN", "currency": "USD", "weight_pct": 10.0, "cost_total": 1000.0,
            "realized_pnl": None, "unrealized_pnl": 50.0, "ticker": "AAA", "days_open": 3,
            "unrealized_pnl_pct": 5.0,
        }
    ]
    block = compact_book_for_prompt(aggregate_book(positions), positions)
    assert block is not None
    serialized = json.dumps(block)
    for forbidden in ("win_rate", "accuracy", "stop_hit_rate", "streak", "avg_pnl"):
        assert forbidden not in serialized
