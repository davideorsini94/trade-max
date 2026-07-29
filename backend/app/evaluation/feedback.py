"""LLM-backed feedback loop (blueprint section 7.2).

After an evaluation scores the pipeline, this module turns each agent's track
record into concrete, imperative lessons (injected into future system prompts by
``BaseAgent``) and writes a short Italian summary of the whole evaluation.

Design notes
------------
* Every LLM call is isolated: a failure for one agent skips only that agent and
  never aborts the loop, and the caller (:mod:`app.evaluation.evaluator`) wraps
  the whole thing so an LLM outage cannot fail the evaluation.
* DB work is done in short synchronous windows around the (slow, awaited) LLM
  calls, so a SQLite write lock is never held across a network round-trip.
* ``get_llm_client`` is imported lazily inside functions to avoid importing the
  ``app.api`` package at module load (which would create an import cycle:
  ``app.api.evaluations`` -> ``evaluator`` -> ``feedback``).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select

from app.db import session_scope
from app.evaluation.features import FEATURE_SPECS
from app.llm.prefs import get_pref
from app.models import AgentFeedback, Analysis, Evaluation, Recommendation, Symbol

logger = logging.getLogger(__name__)

# Mirror of the seven pipeline actors (kept local so feedback never imports the
# evaluator module — that would be a cycle).
ANALYST_AGENTS: tuple[str, ...] = (
    "technical",
    "fundamentals",
    "macro_news",
    "corporate_news",
    "sentiment",
)
AGENT_NAMES: tuple[str, ...] = ANALYST_AGENTS + ("synthesizer", "validator")

# Keep only this many of an agent's most recent feedback rows active.
ACTIVE_FEEDBACK_KEEP = 3
MAX_LESSONS = 3
MAX_WORST_CASES = 3
# Feature-aware coaching (blueprint §7 addendum, part 4 of 4): caps on how many
# cumulative signal_conditions and per-case feature values reach the coach
# prompt, to keep this weekly, LLM-backed call's payload bounded.
MAX_SIGNAL_CONDITIONS = 6
MAX_CASE_FEATURES = 6
#: Literal the coach prompt is told to treat as "not enough cumulative history
#: yet -- do not invent a condition" (matches app.evaluation.features' own
#: per-bucket/rank-IC status string).
INSUFFICIENT_DATA = "dati_insufficienti"

_LESSONS_SYSTEM_PROMPT = (
    "You are a performance coach for an AI financial-analysis agent named "
    '"{agent}". You are given its scored track record over the latest weekly '
    "review, its worst misses, the lessons it is already applying, and — once "
    "enough history has accumulated — signal_conditions: deterministic market "
    "conditions (a specific feature/bucket this agent's own signals are drawn "
    "from) that were CUMULATIVELY associated with its calls beating or missing "
    "the market, each with its own sample size (n) and accuracy. Some "
    "worst_cases also carry a 'features' block: the deterministic signals that "
    "were actually true for THAT specific miss. Write at most 3 concrete, "
    "actionable lessons that would measurably improve its next analyses.\n"
    "Rules:\n"
    "- Each lesson: imperative, specific, under 25 words, in English.\n"
    "- Reference the observed error patterns; do NOT give generic advice.\n"
    "- When signal_conditions has entries, tie at least one lesson to a named "
    "condition (its feature and bucket) instead of a vague trend.\n"
    '- signal_conditions being the string "dati_insufficienti" means there is '
    "not yet enough cumulative history to trust any one condition: do NOT "
    "invent or assume one — base your lessons on worst_cases and metrics only.\n"
    "- execution_stats (present only for the synthesizer, and only once enough "
    "positions have closed) describes what actually happened to the desk's own "
    "simulated trades: how often the stop-loss was hit versus the take-profit, "
    "and the average max adverse/favorable excursion (how far price moved "
    "against/for the position before the exit). Use it to sharpen level-setting "
    "specifically — e.g. if the stop-hit rate is high while the average "
    "favorable excursion is also large, the stops are too tight for the "
    "horizon. Absent means not enough closed positions: do not speculate.\n"
    "- Do not repeat lessons it already applies unless you sharpen them.\n"
    "- Invent no facts beyond the provided data.\n"
    "- lessons_it is read by a NON-EXPERT with no finance background: write it in "
    "plain, clear Italian as one short paragraph. Keep the correct financial term but "
    "explain it briefly in parentheses the first time it appears (for example: "
    "drawdown (quanto il prezzo è sceso dal suo massimo)), use short sentences, never "
    "leave an acronym unexplained, and end with what it means in practice.\n"
    "Output STRICT JSON only, no prose outside JSON:\n"
    '{{"lessons": ["...", "..."], "lessons_it": "Italian translation of the '
    'lessons, as one short paragraph"}}\n'
    'If there is not enough signal to draw lessons, return {{"lessons": [], '
    '"lessons_it": ""}}.'
)

_REPORT_SYSTEM_PROMPT = (
    "You are the reporting analyst of a conservative retail stock-advisory desk. "
    "Summarize the weekly evaluation results for the end user in ITALIAN. "
    "Write 5 to 8 plain sentences (no markdown, no bullet lists, no headings). "
    "Cover overall accuracy, average realized return, hypothetical P&L, the best "
    "and worst symbols, and which agents performed best and worst. Be factual and "
    "measured; this is informational only and not investment advice.\n"
    "Write for a NON-EXPERT reader with no finance background: use the correct term "
    "but explain each metric in plain Italian in parentheses the FIRST time it "
    "appears — for example accuratezza (quante previsioni si sono rivelate corrette), "
    "rendimento realizzato (quanto avrebbero reso davvero in media i titoli seguiti), "
    "P&L ipotetico (il guadagno o la perdita teorici se si fossero seguiti tutti i "
    "consigli). Use short, clear sentences and end with what the results mean in "
    "practice for the user.\n"
    'Output STRICT JSON only: {"report_it": "..."}.'
)


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _round(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None else None


def _default_pref() -> tuple[str | None, str | None]:
    """Resolve the configured ``(provider, model)`` for the ``default`` slot.

    Feedback/report generation are desk-wide (not per-actor), so they follow the
    ``default`` preference; ``(None, None)`` falls back to the env config.
    """
    pref = get_pref("default")
    return pref if pref is not None else (None, None)


# --------------------------------------------------------------------------- #
# Active lessons (consumed by every agent's system prompt)
# --------------------------------------------------------------------------- #


def get_active_lessons(db, agent_name: str, limit: int = 5) -> list[str]:
    """Active English lessons for ``agent_name``, most recent first, truncated."""
    rows = (
        db.execute(
            select(AgentFeedback.lessons_json)
            .where(
                AgentFeedback.agent_name == agent_name,
                AgentFeedback.is_active.is_(True),
            )
            .order_by(AgentFeedback.created_at.desc(), AgentFeedback.id.desc())
        )
        .scalars()
        .all()
    )

    lessons: list[str] = []
    for lessons_json in rows:
        try:
            parsed = json.loads(lessons_json) if lessons_json else []
        except (json.JSONDecodeError, TypeError):
            parsed = []
        if not isinstance(parsed, list):
            continue
        for item in parsed:
            if isinstance(item, str) and item.strip():
                lessons.append(item.strip())
                if len(lessons) >= limit:
                    return lessons
    return lessons[:limit]


# --------------------------------------------------------------------------- #
# Feature-aware coaching (blueprint §7 addendum, part 4 of 4)
# --------------------------------------------------------------------------- #
# Both helpers below are PURE (no DB, no I/O): they only reshape data already
# read elsewhere (compute_feature_stats' cumulative stats, and a single rec's
# own features_json snapshot), which is what makes them directly testable.


def _signal_conditions_for_agent(feature_stats: dict | None, agent: str) -> list[dict] | str:
    """Up to ``MAX_SIGNAL_CONDITIONS`` cumulative "ok" bucket conditions for
    ``agent``'s own features, ranked by how far their accuracy sits from a coin
    flip (most informative first); or the literal string
    :data:`INSUFFICIENT_DATA` when none qualify yet (never invented).
    """
    features = feature_stats.get("features") if isinstance(feature_stats, dict) else None
    if not isinstance(features, dict):
        return INSUFFICIENT_DATA

    conditions: list[dict[str, Any]] = []
    for spec in FEATURE_SPECS:
        if spec.agent != agent:
            continue
        entry = features.get(spec.name)
        buckets = entry.get("buckets") if isinstance(entry, dict) else None
        if not isinstance(buckets, dict):
            continue
        for bucket_name, stat in buckets.items():
            if not isinstance(stat, dict) or stat.get("status") != "ok":
                continue
            conditions.append(
                {
                    "feature": spec.name,
                    "bucket": bucket_name,
                    "n": stat.get("n"),
                    "accuracy": stat.get("accuracy"),
                    "avg_excess_return_pct": stat.get("avg_excess_return_pct"),
                }
            )

    if not conditions:
        return INSUFFICIENT_DATA
    conditions.sort(key=lambda c: abs((c.get("accuracy") or 0.5) - 0.5), reverse=True)
    return conditions[:MAX_SIGNAL_CONDITIONS]


def _feature_values_for_agent(features_json: str | None, agent: str) -> dict[str, Any]:
    """Up to ``MAX_CASE_FEATURES`` raw feature values belonging to ``agent``,
    read from one recommendation's own ``features_json`` snapshot. Returns an
    empty dict (never raises) when the snapshot is missing, malformed, or has
    nothing for this agent -- e.g. every pre-existing recommendation, and the
    synthesizer (no feature is attributed to it in ``FEATURE_SPECS``).
    """
    if not features_json:
        return {}
    try:
        snapshot = json.loads(features_json)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(snapshot, dict):
        return {}

    values: dict[str, Any] = {}
    for spec in FEATURE_SPECS:
        if spec.agent != agent:
            continue
        value = snapshot.get(spec.name)
        if value is not None:
            values[spec.name] = value
        if len(values) >= MAX_CASE_FEATURES:
            break
    return values


def _execution_stats_for_coach(sim_stats: dict | None) -> dict[str, Any] | None:
    """Statistiche di PERCORSO del libro simulato, solo se superano i cancelli.

    Portano al coach un'informazione che il rendimento puntuale a 7 giorni e a
    orizzonte non possono contenere: quante volte lo stop è stato colpito, e
    quanto in profondità il prezzo è andato contro la posizione prima di
    risalire (MAE). Serve a rendere apprendibile una lezione del tipo "gli stop
    a due volte l'ATR vengono colpiti e poi il prezzo recupera".

    Restituisce ``None`` quando il campione non supera le soglie: sotto quella
    numerosità un tasso di stop colpiti misura il denominatore, non la
    strategia — è la lezione della metrica che oscillava.
    """
    if not isinstance(sim_stats, dict) or sim_stats.get("status") != "ok":
        return None
    return {
        "n_closed_positions": sim_stats.get("n"),
        "stop_hit_rate": sim_stats.get("stop_hit_rate"),
        "take_profit_hit_rate": sim_stats.get("tp_hit_rate"),
        "avg_max_adverse_excursion_pct": sim_stats.get("avg_mae_pct"),
        "avg_max_favorable_excursion_pct": sim_stats.get("avg_mfe_pct"),
        "avg_realized_pnl_pct_by_exit": sim_stats.get("avg_pnl_pct_by_reason"),
    }


def _build_agent_payload(
    agent: str,
    metrics: dict,
    worst_cases: list[dict],
    active_lessons: list[str],
    feature_stats: dict | None,
    sim_stats: dict | None = None,
) -> dict[str, Any]:
    """Pure: assemble one agent's full coaching payload (testable without a DB)."""
    payload: dict[str, Any] = {
        "metrics": {
            "accuracy": metrics.get("accuracy"),
            "avg_signal_error": metrics.get("avg_signal_error"),
            "n_samples": int(metrics.get("n_samples") or 0),
        },
        "worst_cases": worst_cases,
        "active_lessons": active_lessons,
        "signal_conditions": _signal_conditions_for_agent(feature_stats, agent),
    }
    # Solo al sintetizzatore: è l'unico attore che decide stop_loss_price,
    # take_profit_price e horizon_days, quindi è l'unico che può agire su queste
    # statistiche. Darle anche agli altri sei costerebbe token senza cambiare
    # nulla di ciò che possono fare.
    if agent == "synthesizer":
        execution = _execution_stats_for_coach(sim_stats)
        if execution is not None:
            payload["execution_stats"] = execution
    return payload


# --------------------------------------------------------------------------- #
# Worst-case gathering (compact examples fed to the coach prompt)
# --------------------------------------------------------------------------- #


def _gather_worst_cases(db, evaluation_id: int) -> dict[str, list[dict]]:
    """For each agent, its ``MAX_WORST_CASES`` worst evaluated cases this period.

    Analysts are ranked by signal error (biggest miss first); the synthesizer by
    realized outcome_score (worst outcome first). The VALIDATOR is ranked on the
    counterfactual instead — see the comment where its case is built.
    """
    # Lazy import: ``app.evaluation.evaluator`` imports THIS module, so pulling it
    # in at module level would be a cycle (same reason get_llm_client is lazy).
    from app.evaluation.evaluator import _outcome_score, _proposed_action

    recs = (
        db.execute(
            select(Recommendation).where(Recommendation.evaluation_id == evaluation_id)
        )
        .scalars()
        .all()
    )
    if not recs:
        return {name: [] for name in AGENT_NAMES}

    # ticker lookup
    symbol_ids = {rec.symbol_id for rec in recs}
    tickers: dict[int, str] = {}
    for symbol_id in symbol_ids:
        symbol = db.get(Symbol, symbol_id)
        tickers[symbol_id] = symbol.ticker if symbol is not None else str(symbol_id)

    # analyses for all involved runs, keyed by (run_id, agent_name)
    run_ids = [rec.run_id for rec in recs]
    by_run_agent: dict[tuple[int, str], Analysis] = {}
    if run_ids:
        for analysis in (
            db.execute(select(Analysis).where(Analysis.run_id.in_(run_ids))).scalars().all()
        ):
            by_run_agent[(analysis.run_id, analysis.agent_name)] = analysis

    analyst_cases: dict[str, list[dict]] = {name: [] for name in ANALYST_AGENTS}
    synth_cases: list[dict] = []
    validator_cases: list[dict] = []

    for rec in recs:
        ret = rec.realized_return_7d
        score = rec.outcome_score
        if ret is None or score is None:
            continue
        ticker = tickers.get(rec.symbol_id, str(rec.symbol_id))

        for agent in ANALYST_AGENTS:
            analysis = by_run_agent.get((rec.run_id, agent))
            if analysis is None or analysis.signal is None or analysis.status != "OK":
                continue
            signal_error = abs(analysis.signal - _clamp(ret / 5.0))
            case = {
                "ticker": ticker,
                "signal": round(analysis.signal, 3),
                "confidence": _round(analysis.confidence, 3),
                "realized_return_7d": round(ret, 2),
                "outcome_score": round(score, 3),
                "signal_error": round(signal_error, 3),
            }
            features = _feature_values_for_agent(rec.features_json, agent)
            if features:
                case["features"] = features
            analyst_cases[agent].append(case)

        common = {
            "ticker": ticker,
            "action": rec.action,
            "confidence": _round(rec.confidence, 3),
            "realized_return_7d": round(ret, 2),
            "outcome_score": round(score, 3),
        }
        synth_case = dict(common)
        synth_features = _feature_values_for_agent(rec.features_json, "synthesizer")
        if synth_features:
            synth_case["features"] = synth_features
        synth_cases.append(synth_case)

        # The validator's own worst case is NOT the one with the worst final
        # outcome: when it blocks a proposal the final action becomes HOLD, which
        # scores well in a quiet market, so a wrongly-blocked winner used to look
        # like a success and never reached the coach. Rank it on the
        # COUNTERFACTUAL instead — what the blocked proposal would have earned.
        validator_case = {**common, "verdict": rec.validator_verdict}
        proposed = _proposed_action(rec)
        if proposed != rec.action:
            counter = _outcome_score(proposed, ret)
            validator_case["proposed_action"] = proposed
            validator_case["blocked_proposal_would_have_scored"] = round(counter, 3)
            # Blocking a winner is the validator's error: rank those first.
            validator_case["_rank"] = -counter
        else:
            validator_case["_rank"] = score
        validator_features = _feature_values_for_agent(rec.features_json, "validator")
        if validator_features:
            validator_case["features"] = validator_features
        validator_cases.append(validator_case)

    result: dict[str, list[dict]] = {}
    for agent in ANALYST_AGENTS:
        ranked = sorted(
            analyst_cases[agent], key=lambda case: case["signal_error"], reverse=True
        )[:MAX_WORST_CASES]
        result[agent] = ranked  # signal_error is useful context for the coach
    result["synthesizer"] = sorted(synth_cases, key=lambda case: case["outcome_score"])[
        :MAX_WORST_CASES
    ]
    ranked_validator = sorted(validator_cases, key=lambda case: case["_rank"])[
        :MAX_WORST_CASES
    ]
    # ``_rank`` is an internal sort key, never sent to the LLM.
    result["validator"] = [
        {k: v for k, v in case.items() if k != "_rank"} for case in ranked_validator
    ]
    return result


# --------------------------------------------------------------------------- #
# Defensive coercion of LLM output
# --------------------------------------------------------------------------- #


def _coerce_lessons(data: dict) -> tuple[list[str], str]:
    lessons: list[str] = []
    raw = data.get("lessons") if isinstance(data, dict) else None
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item.strip():
                lessons.append(item.strip())
            elif isinstance(item, dict):
                for value in item.values():
                    if isinstance(value, str) and value.strip():
                        lessons.append(value.strip())
                        break
            if len(lessons) >= MAX_LESSONS:
                break
    elif isinstance(raw, str) and raw.strip():
        lessons.append(raw.strip())

    lessons_it = data.get("lessons_it") if isinstance(data, dict) else None
    if not isinstance(lessons_it, str):
        lessons_it = ""
    return lessons[:MAX_LESSONS], lessons_it.strip()


# --------------------------------------------------------------------------- #
# Lesson generation
# --------------------------------------------------------------------------- #


async def generate_lessons(evaluation_id: int) -> None:
    """Generate + persist per-agent lessons for one evaluation (all seven agents)."""
    # Phase 1: gather everything needed, in a single short read window.
    with session_scope() as db:
        evaluation = db.get(Evaluation, evaluation_id)
        if evaluation is None:
            logger.warning("generate_lessons: evaluation %s not found", evaluation_id)
            return
        try:
            per_agent = json.loads(evaluation.per_agent_json) if evaluation.per_agent_json else {}
        except (json.JSONDecodeError, TypeError):
            per_agent = {}
        if not isinstance(per_agent, dict):
            per_agent = {}

        try:
            feature_stats = (
                json.loads(evaluation.feature_stats_json)
                if evaluation.feature_stats_json
                else None
            )
        except (json.JSONDecodeError, TypeError):
            feature_stats = None
        if not isinstance(feature_stats, dict):
            feature_stats = None

        worst_cases = _gather_worst_cases(db, evaluation_id)

        # Statistiche di percorso del libro simulato, calcolate al momento della
        # lettura come tutto il resto delle metriche oneste (nessuna colonna
        # nuova su Evaluation).
        try:
            from app.engine.sim_book import sim_stats as compute_sim_stats
            from app.engine.sim_trader import closed_position_rows

            book_stats = compute_sim_stats(closed_position_rows(db))
        except Exception:
            logger.warning("Statistiche libro simulato non disponibili", exc_info=True)
            book_stats = None

        payloads: dict[str, dict] = {}
        for agent in AGENT_NAMES:
            metrics = per_agent.get(agent) if isinstance(per_agent.get(agent), dict) else {}
            payloads[agent] = _build_agent_payload(
                agent,
                metrics,
                worst_cases.get(agent, []),
                get_active_lessons(db, agent),
                feature_stats,
                book_stats,
            )

    # Phase 2: one LLM call per agent (no DB session held across the await).
    from app.api.deps import get_llm_client  # lazy: avoids an import cycle

    client = get_llm_client()
    pref_provider, pref_model = _default_pref()
    for agent in AGENT_NAMES:
        payload = payloads[agent]
        system = _LESSONS_SYSTEM_PROMPT.format(agent=agent)
        user = json.dumps(
            {
                "agent": agent,
                "metrics": payload["metrics"],
                "worst_cases": payload["worst_cases"],
                "active_lessons": payload["active_lessons"],
                "signal_conditions": payload["signal_conditions"],
            },
            ensure_ascii=False,
        )
        try:
            data, _provider = await client.complete_json(
                system,
                user,
                temperature=0.3,
                max_tokens=1100,
                provider=pref_provider,
                model=pref_model,
            )
        except Exception:
            logger.exception("Lesson generation failed for agent %s", agent)
            continue

        lessons, lessons_it = _coerce_lessons(data)

        # Phase 3: persist + prune, in a short write window.
        try:
            with session_scope() as db:
                db.add(
                    AgentFeedback(
                        evaluation_id=evaluation_id,
                        agent_name=agent,
                        accuracy=payload["metrics"]["accuracy"],
                        avg_signal_error=payload["metrics"]["avg_signal_error"],
                        lessons_json=json.dumps(lessons, ensure_ascii=False),
                        lessons_it=lessons_it,
                        is_active=True,
                    )
                )
                db.flush()
                _deactivate_stale_feedback(db, agent)
        except Exception:
            logger.exception("Persisting feedback failed for agent %s", agent)


def _deactivate_stale_feedback(db, agent_name: str) -> None:
    """Keep only the ``ACTIVE_FEEDBACK_KEEP`` newest feedback rows active."""
    rows = (
        db.execute(
            select(AgentFeedback)
            .where(AgentFeedback.agent_name == agent_name)
            .order_by(AgentFeedback.created_at.desc(), AgentFeedback.id.desc())
        )
        .scalars()
        .all()
    )
    for row in rows[ACTIVE_FEEDBACK_KEEP:]:
        if row.is_active:
            row.is_active = False


# --------------------------------------------------------------------------- #
# Italian report
# --------------------------------------------------------------------------- #


async def generate_report_it(evaluation_id: int) -> None:
    """Write a short Italian summary of the evaluation into ``report_it``."""
    with session_scope() as db:
        evaluation = db.get(Evaluation, evaluation_id)
        if evaluation is None:
            logger.warning("generate_report_it: evaluation %s not found", evaluation_id)
            return
        try:
            per_agent = json.loads(evaluation.per_agent_json) if evaluation.per_agent_json else {}
        except (json.JSONDecodeError, TypeError):
            per_agent = {}
        summary = {
            "period_start": evaluation.period_start.isoformat(),
            "period_end": evaluation.period_end.isoformat(),
            "total_recommendations": evaluation.total_recommendations,
            "evaluated_count": evaluation.evaluated_count,
            "accuracy_overall": evaluation.accuracy_overall,
            "avg_realized_return_pct": evaluation.avg_realized_return_pct,
            "hypothetical_pnl_pct": evaluation.hypothetical_pnl_pct,
            "best_symbol": evaluation.best_symbol,
            "worst_symbol": evaluation.worst_symbol,
            "per_agent": per_agent,
        }

    from app.api.deps import get_llm_client  # lazy: avoids an import cycle

    client = get_llm_client()
    pref_provider, pref_model = _default_pref()
    user = json.dumps(summary, ensure_ascii=False)
    try:
        data, _provider = await client.complete_json(
            _REPORT_SYSTEM_PROMPT,
            user,
            temperature=0.3,
            max_tokens=950,
            provider=pref_provider,
            model=pref_model,
        )
    except Exception:
        logger.exception("Report generation failed for evaluation %s", evaluation_id)
        return

    report_it = data.get("report_it") if isinstance(data, dict) else None
    if not isinstance(report_it, str) or not report_it.strip():
        return

    with session_scope() as db:
        evaluation = db.get(Evaluation, evaluation_id)
        if evaluation is not None:
            evaluation.report_it = report_it.strip()
