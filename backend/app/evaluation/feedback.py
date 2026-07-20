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

from sqlalchemy import select

from app.db import session_scope
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

_LESSONS_SYSTEM_PROMPT = (
    "You are a performance coach for an AI financial-analysis agent named "
    '"{agent}". You are given its scored track record over the latest weekly '
    "review, its worst misses, and the lessons it is already applying. Write at "
    "most 3 concrete, actionable lessons that would measurably improve its next "
    "analyses.\n"
    "Rules:\n"
    "- Each lesson: imperative, specific, under 25 words, in English.\n"
    "- Reference the observed error patterns; do NOT give generic advice.\n"
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
# Worst-case gathering (compact examples fed to the coach prompt)
# --------------------------------------------------------------------------- #


def _gather_worst_cases(db, evaluation_id: int) -> dict[str, list[dict]]:
    """For each agent, its ``MAX_WORST_CASES`` worst evaluated cases this period.

    Analysts are ranked by signal error (biggest miss first); synthesizer and
    validator by realized outcome_score (worst outcome first).
    """
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
            analyst_cases[agent].append(
                {
                    "ticker": ticker,
                    "signal": round(analysis.signal, 3),
                    "confidence": _round(analysis.confidence, 3),
                    "realized_return_7d": round(ret, 2),
                    "outcome_score": round(score, 3),
                    "signal_error": round(signal_error, 3),
                }
            )

        common = {
            "ticker": ticker,
            "action": rec.action,
            "confidence": _round(rec.confidence, 3),
            "realized_return_7d": round(ret, 2),
            "outcome_score": round(score, 3),
        }
        synth_cases.append(dict(common))
        validator_cases.append({**common, "verdict": rec.validator_verdict})

    result: dict[str, list[dict]] = {}
    for agent in ANALYST_AGENTS:
        ranked = sorted(
            analyst_cases[agent], key=lambda case: case["signal_error"], reverse=True
        )[:MAX_WORST_CASES]
        result[agent] = ranked  # signal_error is useful context for the coach
    result["synthesizer"] = sorted(synth_cases, key=lambda case: case["outcome_score"])[
        :MAX_WORST_CASES
    ]
    result["validator"] = sorted(validator_cases, key=lambda case: case["outcome_score"])[
        :MAX_WORST_CASES
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

        worst_cases = _gather_worst_cases(db, evaluation_id)
        payloads: dict[str, dict] = {}
        for agent in AGENT_NAMES:
            metrics = per_agent.get(agent) if isinstance(per_agent.get(agent), dict) else {}
            payloads[agent] = {
                "metrics": {
                    "accuracy": metrics.get("accuracy"),
                    "avg_signal_error": metrics.get("avg_signal_error"),
                    "n_samples": int(metrics.get("n_samples") or 0),
                },
                "worst_cases": worst_cases.get(agent, []),
                "active_lessons": get_active_lessons(db, agent),
            }

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
