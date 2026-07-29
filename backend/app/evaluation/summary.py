"""Rolling, honest performance summary for the Performance page.

Why this exists: the accuracy shown in the UI used to be the accuracy of ONE
evaluation batch. Batches are tiny and arbitrary (n = 12, 3, 1, 3, 7, 2, 6 over
ten days, because the evaluation is designed weekly but also runs on demand), so
the displayed number swung 0.92 -> 0.33 -> 0.00 -> 1.00 -> 0.17 with no change
in the pipeline: with n=1 the only possible values ARE 0% and 100%. On top of
that, the same favourites are re-analysed several times a day, so correlated
recommendations were counted as independent samples.

The fix is to stop reading the number off an ``Evaluation`` row and recompute it
at READ time over a rolling window of ``Recommendation`` rows:

* **Rolling window** instead of one batch — ad-hoc extra evaluations stop moving
  the headline number, because the cohort is defined by time, not by which run
  happened to sweep up which rows.
* **Deduplicated** by (symbol, ISO week), reusing the very same helper the
  feature stats use, so one title re-analysed hourly counts once.
* **Correctness recomputed** with the CURRENT rule (``evaluator._is_correct``),
  which makes the whole history definition-consistent even though the HOLD bar
  changed once — no historical row is rewritten, they stay the record of what
  was shown at the time.
* **Honesty gate** with the same floors as the feature stats: below them the
  answer is "not enough data", never a confident-looking 0%.
* A **Wilson interval**, so n=1 reads "0% (0-79%)" and is visibly uninformative,
  and a **continuous** average score, which has no threshold at all and is far
  steadier at small n than a share-of-correct.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evaluation.evaluator import (
    BASE_NORM_PCT,
    _attribute_per_agent,
    _EvalItem,
    _is_correct,
)
from app.evaluation.features import (
    FEATURE_STATS_MIN_N,
    FEATURE_STATS_MIN_SYMBOLS,
    _dedupe_by_symbol_week,
)
from app.models import Recommendation

#: Bumped when the definition of "correct" changes, so a stored Evaluation can be
#: told apart from one scored under a different rule. v1 = HOLD correct above
#: 0.2 (implicit on rows predating the change); v2 = HOLD correct above 0.6.
ACCURACY_METRIC_VERSION = 2

#: Four ISO weeks: long enough to accumulate a usable cohort at the real pace
#: (~10 deduplicated samples/week), short enough that the number describes the
#: current pipeline rather than averaging in every past configuration.
ROLLING_WINDOW_DAYS = 28

#: 95% two-sided normal quantile for the Wilson interval.
WILSON_Z = 1.96

#: Per-agent accuracy is confidence-WEIGHTED, so the binomial model behind Wilson
#: does not apply there; those get the honesty gate and a visible n instead.
AGENT_MIN_SAMPLES = FEATURE_STATS_MIN_N


def wilson_interval(k: int, n: int, z: float = WILSON_Z) -> tuple[float, float]:
    """Wilson score interval for ``k`` successes out of ``n``, clamped to [0, 1].

    Preferred over the textbook normal interval precisely because it behaves at
    the sample sizes this app actually has: at k=0, n=1 it returns roughly
    (0.0, 0.79) instead of the degenerate (0.0, 0.0) that would make "0%" look
    like a measurement.
    """
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    denominator = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    margin = (z / denominator) * math.sqrt(p * (1.0 - p) / n + z * z / (4 * n * n))
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def _cohort(db: Session, window_days: int, now: datetime) -> list[Recommendation]:
    """Scored recommendations inside the window, one per (symbol, ISO week)."""
    cutoff = now - timedelta(days=window_days)
    rows = (
        db.execute(
            select(Recommendation).where(
                Recommendation.evaluated.is_(True),
                Recommendation.outcome_score.isnot(None),
                Recommendation.created_at >= cutoff,
            )
        )
        .scalars()
        .all()
    )
    return _dedupe_by_symbol_week(list(rows))


def _per_agent_rolling(
    db: Session, cohort: list[Recommendation], n_symbols: int
) -> dict[str, dict[str, Any]]:
    """Per-agent metrics over the rolling cohort, gated on sample size.

    Rebuilds ``_EvalItem`` from the persisted columns and hands them to
    ``evaluator._attribute_per_agent`` verbatim — the attribution logic (analyst
    signal error, synthesizer, the validator's counterfactual) lives in exactly
    one place and is not duplicated here.
    """
    items = [
        _EvalItem(
            rec=rec,
            ret=rec.realized_return_7d,
            score=rec.outcome_score,
            correct=_is_correct(rec.action, rec.outcome_score),
            norm=BASE_NORM_PCT,
        )
        for rec in cohort
        if rec.realized_return_7d is not None and rec.outcome_score is not None
    ]
    per_agent = _attribute_per_agent(db, items)
    gated: dict[str, dict[str, Any]] = {}
    for agent, metrics in per_agent.items():
        n_samples = int(metrics.get("n_samples") or 0)
        enough = n_samples >= AGENT_MIN_SAMPLES and n_symbols >= FEATURE_STATS_MIN_SYMBOLS
        gated[agent] = {
            **metrics,
            "status": "ok" if enough else "dati_insufficienti",
            # Never present an accuracy the sample cannot support.
            "accuracy": metrics.get("accuracy") if enough else None,
        }
    return gated


def compute_performance_summary(
    db: Session,
    window_days: int = ROLLING_WINDOW_DAYS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Rolling performance summary; never raises on an empty/thin cohort.

    ``accuracy`` is only filled when the cohort clears BOTH honesty floors
    (enough samples AND enough distinct symbols); the Wilson interval and the
    continuous ``avg_outcome_score`` are returned whenever there is at least one
    sample, because they degrade honestly instead of pretending precision.
    """
    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=window_days)
    n_raw = (
        db.execute(
            select(Recommendation).where(
                Recommendation.evaluated.is_(True),
                Recommendation.outcome_score.isnot(None),
                Recommendation.created_at >= cutoff,
            )
        )
        .scalars()
        .all()
    )
    cohort = _dedupe_by_symbol_week(list(n_raw))

    n = len(cohort)
    n_symbols = len({rec.symbol_id for rec in cohort})
    action_mix = Counter(rec.action for rec in cohort)
    enough = n >= FEATURE_STATS_MIN_N and n_symbols >= FEATURE_STATS_MIN_SYMBOLS

    k_correct = sum(1 for rec in cohort if _is_correct(rec.action, rec.outcome_score))
    ci_low, ci_high = wilson_interval(k_correct, n) if n else (None, None)
    avg_score = sum(rec.outcome_score for rec in cohort) / n if n else None

    result: dict[str, Any] = {
        "status": "ok" if enough else "dati_insufficienti",
        "window_days": window_days,
        "metric_version": ACCURACY_METRIC_VERSION,
        "n": n,
        "n_raw": len(n_raw),
        "n_symbols": n_symbols,
        "k_correct": k_correct,
        "accuracy": round(k_correct / n, 4) if (enough and n) else None,
        "ci_low": round(ci_low, 4) if ci_low is not None else None,
        "ci_high": round(ci_high, 4) if ci_high is not None else None,
        "avg_outcome_score": round(avg_score, 4) if avg_score is not None else None,
        "action_mix": dict(action_mix),
        # All-HOLD means the number says more about how calm the market was than
        # about stock picking; the UI warns while this holds.
        "hold_only": bool(n) and set(action_mix) <= {"HOLD"},
        "min_n": FEATURE_STATS_MIN_N,
        "min_symbols": FEATURE_STATS_MIN_SYMBOLS,
    }
    result["per_agent"] = _per_agent_rolling(db, cohort, n_symbols) if n else {}
    return result
