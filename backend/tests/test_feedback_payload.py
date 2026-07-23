"""Offline tests for feature-aware coaching payload construction (blueprint §7
addendum, part 4). Pure unit tests -- no DB, no network, no LLM: these three
helpers only reshape data already produced elsewhere (compute_feature_stats'
shape, and a single rec's own features_json snapshot).
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from app.evaluation.feedback import (
    INSUFFICIENT_DATA,
    MAX_SIGNAL_CONDITIONS,
    _build_agent_payload,
    _feature_values_for_agent,
    _gather_worst_cases,
    _signal_conditions_for_agent,
)
from app.models import Analysis, AnalysisRun, Evaluation, Recommendation, Symbol


def _feature_stats_with(**per_feature_buckets: dict) -> dict:
    """Build a compute_feature_stats-shaped dict from {feature_name: {bucket: stat}}."""
    return {
        "cohort_n": 100,
        "features": {
            name: {"buckets": buckets} for name, buckets in per_feature_buckets.items()
        },
    }


# --------------------------------------------------------------------------- #
# _signal_conditions_for_agent
# --------------------------------------------------------------------------- #


def test_signal_conditions_filters_to_the_requested_agent_only():
    # rel_benchmark_30d_pct/rel_sector_30d_pct -> technical; eps_rev_cy_30d_pct -> fundamentals.
    stats = _feature_stats_with(
        rel_benchmark_30d_pct={"positivo": {"status": "ok", "n": 20, "n_symbols": 5, "accuracy": 0.7, "avg_excess_return_pct": 2.1}},
        eps_rev_cy_30d_pct={"positivo": {"status": "ok", "n": 20, "n_symbols": 5, "accuracy": 0.65, "avg_excess_return_pct": 1.5}},
    )
    conditions = _signal_conditions_for_agent(stats, "technical")
    assert conditions != INSUFFICIENT_DATA
    assert all(c["feature"] == "rel_benchmark_30d_pct" for c in conditions)
    # fundamentals' own feature must not leak into technical's conditions.
    assert not any(c["feature"] == "eps_rev_cy_30d_pct" for c in conditions)


def test_signal_conditions_ignores_non_ok_buckets():
    stats = _feature_stats_with(
        rel_benchmark_30d_pct={
            "positivo": {"status": "ok", "n": 20, "n_symbols": 5, "accuracy": 0.7, "avg_excess_return_pct": 2.1},
            "negativo": {"status": "dati_insufficienti", "n": 3, "n_symbols": 1},
        }
    )
    conditions = _signal_conditions_for_agent(stats, "technical")
    assert len(conditions) == 1
    assert conditions[0]["bucket"] == "positivo"


def test_signal_conditions_insufficient_when_nothing_qualifies():
    stats = _feature_stats_with(
        rel_benchmark_30d_pct={"positivo": {"status": "dati_insufficienti", "n": 3, "n_symbols": 1}}
    )
    assert _signal_conditions_for_agent(stats, "technical") == INSUFFICIENT_DATA


def test_signal_conditions_insufficient_when_feature_stats_is_none():
    assert _signal_conditions_for_agent(None, "technical") == INSUFFICIENT_DATA


def test_signal_conditions_capped_and_sorted_by_informativeness():
    # 8 qualifying buckets across technical's 4 features (2 buckets each) so the
    # cap (MAX_SIGNAL_CONDITIONS=6) actually has to trim something.
    buckets = {
        "positivo": {"status": "ok", "n": 20, "n_symbols": 5, "accuracy": 0.51, "avg_excess_return_pct": 0.1},
        "negativo": {"status": "ok", "n": 20, "n_symbols": 5, "accuracy": 0.9, "avg_excess_return_pct": 5.0},
    }
    stats = _feature_stats_with(
        rel_benchmark_30d_pct=buckets,
        rel_benchmark_90d_pct=buckets,
        rel_sector_30d_pct=buckets,
        rel_sector_90d_pct=buckets,
    )
    conditions = _signal_conditions_for_agent(stats, "technical")
    assert conditions != INSUFFICIENT_DATA
    assert len(conditions) == MAX_SIGNAL_CONDITIONS
    # Most-informative-first: an 0.9-accuracy bucket must precede a 0.51 one.
    assert conditions[0]["accuracy"] == 0.9


# --------------------------------------------------------------------------- #
# _feature_values_for_agent
# --------------------------------------------------------------------------- #


def test_feature_values_for_agent_filters_by_agent_and_skips_none():
    snapshot = {
        "v": 1,
        "rel_benchmark_30d_pct": 3.2,       # technical
        "rel_sector_30d_pct": None,          # technical, but missing -> excluded
        "eps_rev_cy_30d_pct": 1.1,           # fundamentals -> must not leak in
    }
    values = _feature_values_for_agent(json.dumps(snapshot), "technical")
    assert values == {"rel_benchmark_30d_pct": 3.2}


def test_feature_values_for_agent_empty_on_malformed_or_missing_json():
    assert _feature_values_for_agent(None, "technical") == {}
    assert _feature_values_for_agent("not json", "technical") == {}
    assert _feature_values_for_agent(json.dumps([1, 2, 3]), "technical") == {}


def test_feature_values_for_agent_empty_for_synthesizer_with_no_attributed_features():
    snapshot = {"rel_benchmark_30d_pct": 3.2, "vix_level": 16.9}
    assert _feature_values_for_agent(json.dumps(snapshot), "synthesizer") == {}


# --------------------------------------------------------------------------- #
# _build_agent_payload
# --------------------------------------------------------------------------- #


def test_build_agent_payload_shape_and_gating_passthrough():
    stats = _feature_stats_with(
        rel_benchmark_30d_pct={"positivo": {"status": "ok", "n": 20, "n_symbols": 5, "accuracy": 0.7, "avg_excess_return_pct": 2.1}}
    )
    payload = _build_agent_payload(
        "technical",
        {"accuracy": 0.55, "avg_signal_error": 0.3, "n_samples": 12},
        worst_cases=[{"ticker": "AAPL", "signal_error": 0.4}],
        active_lessons=["Trust the trend more."],
        feature_stats=stats,
    )
    assert payload["metrics"] == {"accuracy": 0.55, "avg_signal_error": 0.3, "n_samples": 12}
    assert payload["worst_cases"] == [{"ticker": "AAPL", "signal_error": 0.4}]
    assert payload["active_lessons"] == ["Trust the trend more."]
    assert payload["signal_conditions"] != INSUFFICIENT_DATA
    assert payload["signal_conditions"][0]["feature"] == "rel_benchmark_30d_pct"


def test_build_agent_payload_insufficient_data_passthrough_when_stats_absent():
    payload = _build_agent_payload(
        "technical",
        {"accuracy": None, "avg_signal_error": None, "n_samples": 0},
        worst_cases=[],
        active_lessons=[],
        feature_stats=None,
    )
    assert payload["signal_conditions"] == INSUFFICIENT_DATA
    assert payload["metrics"]["n_samples"] == 0


# --------------------------------------------------------------------------- #
# _gather_worst_cases: end-to-end feature enrichment (seeded DB)
# --------------------------------------------------------------------------- #


def test_gather_worst_cases_enriches_cases_with_agent_features(
    db_session_factory: sessionmaker[Session],
) -> None:
    db = db_session_factory()
    try:
        symbol = Symbol(ticker="AAPL", name="Apple Inc.", exchange="NASDAQ", currency="USD")
        db.add(symbol)
        db.commit()
        db.refresh(symbol)

        evaluation = Evaluation(
            period_start=datetime.utcnow(), period_end=datetime.utcnow(), status="COMPLETED",
        )
        db.add(evaluation)
        db.flush()

        run = AnalysisRun(symbol_id=symbol.id, status="COMPLETED", trigger="MANUAL")
        db.add(run)
        db.flush()
        db.add(
            Analysis(
                run_id=run.id, symbol_id=symbol.id, agent_name="technical",
                status="OK", signal=0.4, confidence=0.6,
            )
        )
        rec = Recommendation(
            run_id=run.id,
            symbol_id=symbol.id,
            action="BUY",
            sizing_strategy="DCA",
            confidence=0.6,
            validator_verdict="APPROVE",
            created_at=datetime.utcnow(),
            evaluated=True,
            realized_return_7d=-3.0,
            outcome_score=-0.6,
            evaluation_id=evaluation.id,
            features_json=json.dumps(
                {"rel_benchmark_30d_pct": -5.2, "vix_level": 28.0}  # vix -> macro_news, not technical
            ),
        )
        db.add(rec)
        db.commit()

        worst_cases = _gather_worst_cases(db, evaluation_id=evaluation.id)

        technical_case = worst_cases["technical"][0]
        assert technical_case["features"] == {"rel_benchmark_30d_pct": -5.2}
        # synthesizer has no features attributed to it -> no "features" key at all.
        assert "features" not in worst_cases["synthesizer"][0]
    finally:
        db.close()
