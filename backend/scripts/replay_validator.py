"""Replay the risk validator over historical BUY proposals (A/B on the prompt).

Why this exists: the validator's behaviour is produced by a local LLM, so a unit
test cannot prove "a well-supported BUY is no longer downgraded mechanically".
This script re-asks the CURRENT validator prompt about proposals it already
judged in the past, using the SAME inputs, and prints old verdict vs new. Any
difference is attributable to the prompt/limits, not to market drift.

Inputs are reconstructed from what is persisted:
* the proposal            -> ``Recommendation.synthesizer_json``
* the five analyst reports -> ``Analysis.output_json`` for that run
* the risk metrics        -> ``Recommendation.features_json`` (the deterministic
  snapshot; the live ``risk_metrics`` dict itself is never persisted)

Read-only: nothing is written back to the DB.

Usage (inside the container, with Ollama reachable):
    python -m scripts.replay_validator --limit 8
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter

from sqlalchemy import select

from app.agents.validator import RiskValidatorAgent
from app.db import session_scope
from app.models import Analysis, Recommendation, Symbol

#: features_json key -> risk_metrics key. Only the metrics the validator prompt
#: actually reads; anything missing stays absent (never invented).
_FEATURE_TO_RISK_METRIC: dict[str, str] = {
    "atr_pct": "atr_pct",
    "drawdown_90d_pct": "drawdown_90d_pct",
    "beta": "beta",
    "distance_from_sma200_pct": "distance_from_sma200_pct",
    "days_to_next_earnings": "days_to_next_earnings",
    "vix_level": "vix_level",
    "vix_change_30d_pct": "vix_change_30d_pct",
    "credit_hyg_lqd_ratio_change_30d_pct": "credit_hyg_lqd_ratio_change_30d_pct",
    "max_open_position_correlation_90d": "max_open_position_correlation_90d",
    "correlation_alert": "correlation_alert",
}

_ANALYSTS = ("technical", "fundamentals", "macro_news", "corporate_news", "sentiment")


def _loads(raw: str | None) -> dict:
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _collect_cases(limit: int) -> list[dict]:
    """Historical BUY proposals with enough persisted context to replay."""
    cases: list[dict] = []
    with session_scope() as db:
        recs = (
            db.execute(select(Recommendation).order_by(Recommendation.created_at.desc()))
            .scalars()
            .all()
        )
        for rec in recs:
            proposal = _loads(rec.synthesizer_json)
            if proposal.get("action") != "BUY" or not rec.features_json:
                continue
            features = _loads(rec.features_json)
            risk_metrics = {
                metric: features[key]
                for key, metric in _FEATURE_TO_RISK_METRIC.items()
                if features.get(key) is not None
            }
            rows = (
                db.execute(select(Analysis).where(Analysis.run_id == rec.run_id))
                .scalars()
                .all()
            )
            analyst_outputs = {
                a.agent_name: _loads(a.output_json)
                for a in rows
                if a.agent_name in _ANALYSTS and a.status == "OK"
            }
            if len(analyst_outputs) < 3:
                continue  # too little context to be a fair replay
            old = next((a for a in rows if a.agent_name == "validator"), None)
            old_out = _loads(old.output_json) if old else {}
            symbol = db.get(Symbol, rec.symbol_id)
            cases.append(
                {
                    "ticker": symbol.ticker if symbol else str(rec.symbol_id),
                    "proposal": proposal,
                    "analyst_outputs": {k: analyst_outputs.get(k) for k in _ANALYSTS},
                    "risk_metrics": risk_metrics,
                    "old_verdict": rec.validator_verdict,
                    "old_revised": old_out.get("revised_action"),
                    "old_adj": old_out.get("confidence_adjustment"),
                    "final_action": rec.action,
                }
            )
            if len(cases) >= limit:
                break
    return cases


async def _replay(cases: list[dict]) -> None:
    from app.api.deps import get_llm_client

    llm = get_llm_client()
    agent = RiskValidatorAgent()
    old_counter: Counter = Counter()
    new_counter: Counter = Counter()
    kept_buy = 0

    header = f"{'TICKER':10} {'CONF':>5} | {'PRIMA':<26} | {'DOPO':<26}"
    print(header)
    print("-" * len(header))

    for case in cases:
        conf = case["proposal"].get("confidence")
        old_desc = f"{case['old_verdict']}->{case['old_revised'] or '-'} adj={case['old_adj']}"
        old_counter[f"{case['old_verdict']}/{case['old_revised'] or '-'}"] += 1
        try:
            result = await agent.run(
                proposal=case["proposal"],
                analyst_outputs=case["analyst_outputs"],
                risk_metrics=case["risk_metrics"],
                lessons=[],
                llm=llm,
            )
            out = result.output
            verdict = out.get("verdict")
            revised = out.get("revised_action")
            adj = out.get("confidence_adjustment")
            new_desc = f"{verdict}->{revised or '-'} adj={adj}"
            new_counter[f"{verdict}/{revised or '-'}"] += 1
            # Would the BUY survive the validator itself?
            if verdict == "APPROVE" or not revised or revised == "BUY":
                kept_buy += 1
        except Exception as exc:  # a replay failure must not abort the batch
            new_desc = f"ERRORE: {type(exc).__name__}"
            new_counter["errore"] += 1
        print(f"{case['ticker']:10} {conf!s:>5} | {old_desc:<26} | {new_desc:<26}")

    print()
    print("PRIMA:", dict(old_counter))
    print("DOPO :", dict(new_counter))
    print(f"\nBUY non declassati dal validatore: {kept_buy}/{len(cases)} (prima: 0)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=8, help="quanti casi replicare")
    args = parser.parse_args()

    cases = _collect_cases(args.limit)
    if not cases:
        print("Nessuna proposta BUY storica con contesto sufficiente da replicare.")
        return
    print(f"Replay di {len(cases)} proposte BUY storiche col prompt attuale.\n")
    asyncio.run(_replay(cases))


if __name__ == "__main__":
    main()
