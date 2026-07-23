"""Offline tests for cumulative per-feature validation (blueprint §7 addendum, part 3).

Pure DB tests (no network, no LLM): seeds ``Recommendation`` rows directly with
a synthetic ``features_json``/``excess_return_h``, exercising
``compute_feature_stats`` and its dedupe/gating logic in isolation from the
rest of the evaluation pipeline.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.evaluation.features import (
    FEATURE_STATS_MIN_N,
    FEATURE_STATS_MIN_N_FOR_IC,
    FEATURE_STATS_MIN_SYMBOLS,
    _dedupe_by_symbol_week,
    compute_feature_stats,
)
from app.models import AnalysisRun, Recommendation, Symbol


def _seed_symbol(db: Session, ticker: str) -> int:
    symbol = Symbol(ticker=ticker, name=f"{ticker} Inc.", exchange="NASDAQ", currency="USD")
    db.add(symbol)
    db.commit()
    db.refresh(symbol)
    return symbol.id


def _seed_rec(
    db: Session,
    *,
    symbol_id: int,
    created_at: datetime,
    excess_return_h: float | None,
    features: dict,
) -> Recommendation:
    run = AnalysisRun(symbol_id=symbol_id, status="COMPLETED", trigger="MANUAL")
    db.add(run)
    db.flush()
    rec = Recommendation(
        run_id=run.id,
        symbol_id=symbol_id,
        action="BUY",
        sizing_strategy="DCA",
        confidence=0.6,
        validator_verdict="APPROVE",
        created_at=created_at,
        evaluated_h=True,
        excess_return_h=excess_return_h,
        features_json=json.dumps(features),
    )
    db.add(rec)
    db.commit()
    return rec


# --------------------------------------------------------------------------- #
# 1. Dedupe: two recs, same symbol + same ISO week -> one sample (the earliest)
# --------------------------------------------------------------------------- #


def test_dedupe_by_symbol_week_keeps_earliest(db_session_factory: sessionmaker[Session]) -> None:
    db = db_session_factory()
    try:
        symbol_id = _seed_symbol(db, "AAPL")
        base = datetime(2026, 3, 10)  # a Tuesday; both recs land in the same ISO week
        rec_early = _seed_rec(
            db, symbol_id=symbol_id, created_at=base,
            excess_return_h=1.0, features={"v": 1, "rsi14_bucket": "x"},
        )
        _seed_rec(
            db, symbol_id=symbol_id, created_at=base + timedelta(hours=6),
            excess_return_h=2.0, features={"v": 1, "rsi14_bucket": "x"},
        )

        deduped = _dedupe_by_symbol_week([rec_early])  # sanity: single-item passthrough
        assert len(deduped) == 1

        all_recs = db.query(Recommendation).all()
        deduped_all = _dedupe_by_symbol_week(all_recs)
        assert len(deduped_all) == 1
        assert deduped_all[0].id == rec_early.id  # the earlier of the two survives
    finally:
        db.close()


def test_dedupe_by_symbol_week_keeps_different_weeks_and_symbols(
    db_session_factory: sessionmaker[Session],
) -> None:
    db = db_session_factory()
    try:
        sym_a = _seed_symbol(db, "AAPL")
        sym_b = _seed_symbol(db, "MSFT")
        base = datetime(2026, 3, 10)
        _seed_rec(db, symbol_id=sym_a, created_at=base, excess_return_h=1.0, features={})
        _seed_rec(db, symbol_id=sym_a, created_at=base + timedelta(days=14), excess_return_h=1.0, features={})
        _seed_rec(db, symbol_id=sym_b, created_at=base, excess_return_h=1.0, features={})

        all_recs = db.query(Recommendation).all()
        deduped = _dedupe_by_symbol_week(all_recs)
        assert len(deduped) == 3  # 2 distinct weeks for AAPL + 1 for MSFT, none collide
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 2. Honesty gating: below floor -> "dati_insufficienti" (with n/n_symbols shown)
# --------------------------------------------------------------------------- #


def _seed_bucketed_cohort(
    db: Session, *, n: int, n_symbols: int, bucket_name: str = "positivo"
) -> None:
    """``n`` samples for ``rel_benchmark_30d_pct`` all in the same bucket, spread
    across ``n_symbols`` distinct symbols (and distinct ISO weeks, so none of
    them collide in the dedupe step)."""
    symbol_ids = [_seed_symbol(db, f"SYM{i}") for i in range(n_symbols)]
    base = datetime(2026, 1, 5)  # a Monday
    for i in range(n):
        _seed_rec(
            db,
            symbol_id=symbol_ids[i % n_symbols],
            created_at=base + timedelta(weeks=i),
            excess_return_h=1.5,
            features={
                "rel_benchmark_30d_pct": 2.0,
                "rel_benchmark_30d_pct_bucket": bucket_name,
            },
        )


def test_bucket_below_min_n_is_insufficient(db_session_factory: sessionmaker[Session]) -> None:
    db = db_session_factory()
    try:
        _seed_bucketed_cohort(db, n=FEATURE_STATS_MIN_N - 1, n_symbols=FEATURE_STATS_MIN_SYMBOLS + 2)
        stats = compute_feature_stats(db)
        bucket = stats["features"]["rel_benchmark_30d_pct"]["buckets"]["positivo"]
        assert bucket["status"] == "dati_insufficienti"
        assert bucket["n"] == FEATURE_STATS_MIN_N - 1
    finally:
        db.close()


def test_bucket_below_min_symbols_is_insufficient_even_with_enough_n(
    db_session_factory: sessionmaker[Session],
) -> None:
    db = db_session_factory()
    try:
        # Plenty of samples, but concentrated in too few symbols.
        _seed_bucketed_cohort(db, n=FEATURE_STATS_MIN_N + 4, n_symbols=FEATURE_STATS_MIN_SYMBOLS - 1)
        stats = compute_feature_stats(db)
        bucket = stats["features"]["rel_benchmark_30d_pct"]["buckets"]["positivo"]
        assert bucket["status"] == "dati_insufficienti"
        assert bucket["n_symbols"] == FEATURE_STATS_MIN_SYMBOLS - 1
    finally:
        db.close()


def test_bucket_clears_both_floors_is_ok(db_session_factory: sessionmaker[Session]) -> None:
    db = db_session_factory()
    try:
        _seed_bucketed_cohort(db, n=FEATURE_STATS_MIN_N, n_symbols=FEATURE_STATS_MIN_SYMBOLS)
        stats = compute_feature_stats(db)
        bucket = stats["features"]["rel_benchmark_30d_pct"]["buckets"]["positivo"]
        assert bucket["status"] == "ok"
        assert bucket["n"] == FEATURE_STATS_MIN_N
        assert bucket["n_symbols"] == FEATURE_STATS_MIN_SYMBOLS
        assert bucket["accuracy"] == pytest.approx(1.0)  # every seeded sample has excess_return_h > 0
        assert bucket["avg_excess_return_pct"] == pytest.approx(1.5)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 3. Spearman rank-IC: sign and gating
# --------------------------------------------------------------------------- #


def test_rank_ic_positive_on_perfectly_monotonic_data(
    db_session_factory: sessionmaker[Session],
) -> None:
    db = db_session_factory()
    try:
        n = FEATURE_STATS_MIN_N_FOR_IC
        symbol_ids = [_seed_symbol(db, f"IC{i}") for i in range(n)]
        base = datetime(2026, 1, 5)
        for i in range(n):
            # rsi14 rises in lockstep with the excess return -> perfect positive rank-IC.
            _seed_rec(
                db,
                symbol_id=symbol_ids[i],
                created_at=base + timedelta(weeks=i),
                excess_return_h=float(i),
                features={"rsi14": float(i)},
            )
        stats = compute_feature_stats(db)
        rank_ic = stats["features"]["rsi14"]["rank_ic"]
        assert rank_ic["status"] == "ok"
        assert rank_ic["n"] == n
        assert rank_ic["value"] == pytest.approx(1.0)
    finally:
        db.close()


def test_rank_ic_below_min_n_is_insufficient(db_session_factory: sessionmaker[Session]) -> None:
    db = db_session_factory()
    try:
        n = FEATURE_STATS_MIN_N_FOR_IC - 1
        symbol_ids = [_seed_symbol(db, f"LOW{i}") for i in range(n)]
        base = datetime(2026, 1, 5)
        for i in range(n):
            _seed_rec(
                db,
                symbol_id=symbol_ids[i],
                created_at=base + timedelta(weeks=i),
                excess_return_h=float(i),
                features={"rsi14": float(i)},
            )
        stats = compute_feature_stats(db)
        rank_ic = stats["features"]["rsi14"]["rank_ic"]
        assert rank_ic["status"] == "dati_insufficienti"
        assert rank_ic["n"] == n
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 4. Empty cohort degrades cleanly
# --------------------------------------------------------------------------- #


def test_empty_cohort_returns_clean_empty_result(db_session_factory: sessionmaker[Session]) -> None:
    db = db_session_factory()
    try:
        stats = compute_feature_stats(db)
        assert stats["cohort_n"] == 0
        assert stats["features"] == {}
        assert "note" in stats
    finally:
        db.close()


def test_rows_without_resolvable_excess_return_are_excluded(
    db_session_factory: sessionmaker[Session],
) -> None:
    db = db_session_factory()
    try:
        symbol_id = _seed_symbol(db, "NOBENCH")
        _seed_rec(
            db, symbol_id=symbol_id, created_at=datetime(2026, 1, 5),
            excess_return_h=None,  # no resolvable benchmark for this one
            features={"rsi14": 50.0},
        )
        stats = compute_feature_stats(db)
        assert stats["cohort_n"] == 1  # counted in the raw cohort...
        assert stats["excess_return_n"] == 0  # ...but excluded from the feature analysis
        assert stats["features"] == {}
    finally:
        db.close()
