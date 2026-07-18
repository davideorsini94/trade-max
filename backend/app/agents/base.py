"""Base agent infrastructure for the multi-agent LLM pipeline (blueprint 5.2/5.3).

This module defines the two data contracts shared with the orchestrator
(``AgentContext`` and ``AgentResult``), the ``BaseAgent`` template every actor
extends, and a set of defensive coercion helpers used by every agent's
``validate_output``.

Design notes
------------
* System prompts are written in English (best model behaviour); every
  user-facing string produced by the models (``summary_it``, ``rationale_it``,
  ``notes_it``) is requested in Italian by the prompts themselves.
* ``build_system_prompt`` is implemented once here and *always* ends with the
  "LESSONS FROM PAST PERFORMANCE REVIEWS" block, so no subclass can forget it.
  Subclasses provide their instruction body via ``_system_body``.
* LLM output is untrusted: every field is coerced/clamped and given a safe,
  conservative default. A malformed model response can never crash the pipeline
  — at worst it degrades to a NEUTRAL / HOLD / REVISE stance with zero
  confidence.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

from app.llm.client import LLMClient
from app.llm.prefs import get_pref
from app.schemas import Stance
from app.schemas import SymbolOut

# --------------------------------------------------------------------------- #
# Shared constants
# --------------------------------------------------------------------------- #

# Canonical enum value-sets sourced from the Pydantic schemas so there is a
# single source of truth for the string literals the models must emit.
STANCE_VALUES: frozenset[str] = frozenset(s.value for s in Stance)
DATA_QUALITY_VALUES: frozenset[str] = frozenset({"GOOD", "PARTIAL", "POOR"})

# Conservative defaults for the common analyst schema.
DEFAULT_STANCE: str = Stance.NEUTRAL.value
DEFAULT_DATA_QUALITY: str = "PARTIAL"

# Maximum list lengths enforced on analyst output (blueprint 5.3).
MAX_KEY_POINTS: int = 5
MAX_RISKS: int = 3

# Shared plain-Italian style block. Injected into EVERY actor's system prompt by
# ``BaseAgent.build_system_prompt`` (before the lessons block), so that every
# user-facing Italian field uses correct financial terminology but always
# explains it so a reader with zero finance background understands.
PLAIN_ITALIAN_STYLE = """\
## ITALIAN OUTPUT STYLE (all *_it fields)
Every Italian field you produce (summary_it, rationale_it, notes_it, lessons_it, \
report_it) is read by an ordinary person with NO finance background. Follow these \
rules for ALL Italian text you write:
- Use the CORRECT financial term — never dumb it down or swap it for a vague word — \
but the FIRST time each term or acronym appears, immediately explain it briefly in \
plain Italian inside parentheses. Example: "RSI a 72 (ipercomprato: il prezzo è \
salito molto in fretta e potrebbe presto correggere)".
- NEVER leave any jargon or acronym unexplained on first use. This includes at \
least: RSI, SMA (media mobile), MACD, ATR, bande di Bollinger, P/E (rapporto \
prezzo/utili), EPS, beta, drawdown, death cross, golden cross, DCA, stop loss, take \
profit, allocazione, volatilità, ipercomprato, ipervenduto, rendimento, P&L. If you \
name it, you explain it the first time.
- Write in SHORT, clear sentences. Use everyday Italian words around the technical \
term; keep English only for the technical term itself.
- ALWAYS end the Italian text with the practical takeaway for the reader — what it \
means for them ("cosa significa in pratica ...").
- Explaining a term once (its first appearance) is enough; do not repeat the same \
parenthetical later in the same text."""


# --------------------------------------------------------------------------- #
# Data contracts (blueprint 5.2)
# --------------------------------------------------------------------------- #


@dataclass
class AgentContext:
    """Everything an analyst agent may need to reason about one symbol.

    The orchestrator builds one ``AgentContext`` per run and hands the same
    instance to each of the four analyst agents; every agent serialises only
    the slice of this context that is relevant to its own remit.
    """

    symbol: SymbolOut
    is_favorite: bool
    price_summary: dict[str, Any]
    indicators: dict[str, Any]
    fundamentals: dict[str, Any]
    macro_news: list[dict[str, Any]]
    corporate_news: list[dict[str, Any]]
    risk_profile: str
    lessons: list[str] = field(default_factory=list)


@dataclass
class AgentResult:
    """The validated output of a single agent invocation.

    ``output`` is the fully coerced structured dict (safe to persist as
    ``analyses.output_json``); ``provider`` is the LLM provider name that
    actually produced the response (for audit / ``llm_provider_used``).
    """

    agent_name: str
    output: dict[str, Any]
    provider: str


# --------------------------------------------------------------------------- #
# Coercion helpers (defensive parsing of untrusted LLM output)
# --------------------------------------------------------------------------- #


def clamp(value: float, lo: float, hi: float) -> float:
    """Clamp ``value`` into the inclusive ``[lo, hi]`` range."""
    return max(lo, min(hi, value))


def coerce_float(value: Any, default: float) -> float:
    """Best-effort conversion of ``value`` to a finite float.

    Booleans (a common malformed value for numeric fields), non-finite floats
    and unparseable strings all fall back to ``default``.
    """
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(result):
        return default
    return result


def coerce_optional_float(value: Any) -> float | None:
    """Parse an optional *positive* price-like value; ``None`` when absent/invalid."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or result <= 0.0:
        return None
    return result


def coerce_int(value: Any, default: int, lo: int, hi: int) -> int:
    """Parse an int (rounding floats) and clamp it to ``[lo, hi]``."""
    if isinstance(value, bool):
        return default
    try:
        result = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, result))


def coerce_str(value: Any, default: str = "") -> str:
    """Return a trimmed string; ``default`` for ``None`` / empty / non-scalar."""
    if value is None:
        return default
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else default
    if isinstance(value, (int, float, bool)):
        return str(value)
    return default


def coerce_optional_str(value: Any) -> str | None:
    """Like :func:`coerce_str` but returns ``None`` instead of an empty string."""
    result = coerce_str(value, "")
    return result or None


def coerce_str_list(value: Any, max_len: int) -> list[str]:
    """Coerce ``value`` into a list of non-empty strings, truncated to ``max_len``.

    Accepts a list (each element stringified/trimmed, empties dropped) or a
    single scalar (wrapped in a one-element list). Anything else yields ``[]``.
    """
    items: list[str]
    if isinstance(value, list):
        items = value
    elif value is None:
        return []
    elif isinstance(value, (str, int, float, bool)):
        items = [value]
    else:
        return []

    result: list[str] = []
    for item in items:
        text = coerce_str(item, "")
        if text:
            result.append(text)
        if len(result) >= max_len:
            break
    return result


def coerce_enum(value: Any, allowed: frozenset[str] | set[str], default: str) -> str:
    """Return ``value`` upper-cased if it is one of ``allowed``, else ``default``."""
    if isinstance(value, str):
        candidate = value.strip().upper()
        if candidate in allowed:
            return candidate
    return default


def _round_floats(value: Any) -> Any:
    """Recursively round floats so noisy tails never reach a prompt.

    Values with ``abs >= 100`` are rounded to 2 decimals, smaller ones to 4, so
    numbers like ``333.739990234375`` collapse to ``333.74`` (and thus far fewer
    prompt tokens). Bools, ints and non-float scalars pass through unchanged;
    non-finite floats are left as-is; dicts/lists/tuples recurse.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return value
        return round(value, 2 if abs(value) >= 100.0 else 4)
    if isinstance(value, dict):
        return {key: _round_floats(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_round_floats(item) for item in value]
    return value


def compact_json(data: Any) -> str:
    """Serialise ``data`` as compact JSON (no whitespace) for user prompts.

    Floats are rounded (2 decimals for ``abs >= 100``, else 4) via
    :func:`_round_floats` to keep the payload small without touching what gets
    persisted. ``default=str`` guarantees non-JSON-native values (e.g. datetimes)
    never raise; ``ensure_ascii=False`` keeps the payload small and readable.
    """
    return json.dumps(
        _round_floats(data), ensure_ascii=False, separators=(",", ":"), default=str
    )


def format_lessons_block(lessons: list[str]) -> str:
    """Render the mandatory feedback-injection block (blueprint 5.2).

    At most five lessons, most recent first (the caller supplies them already
    ordered). When there are none, the literal ``No lessons yet.`` is used so
    the section is always present and well-formed.
    """
    header = (
        "## LESSONS FROM PAST PERFORMANCE REVIEWS\n"
        "Your past predictions were scored against realized market outcomes. "
        "Apply these lessons:"
    )
    cleaned = [coerce_str(lesson, "") for lesson in (lessons or [])]
    cleaned = [lesson for lesson in cleaned if lesson][:5]
    if not cleaned:
        body = "No lessons yet."
    else:
        body = "\n".join(f"{i}. {lesson}" for i, lesson in enumerate(cleaned, start=1))
    return f"{header}\n{body}"


def resolve_llm_pref(agent_name: str) -> tuple[str | None, str | None]:
    """Resolve the configured ``(provider, model)`` for ``agent_name``.

    Delegates to :func:`app.llm.prefs.get_pref` (agent row -> ``default`` row ->
    ``None``); returns ``(None, None)`` when no preference applies, so callers can
    always splat the result into ``complete_json(provider=..., model=...)`` and
    fall back to the env-configured provider/model.
    """
    pref = get_pref(agent_name)
    if pref is None:
        return None, None
    return pref


def symbol_descriptor(symbol: SymbolOut) -> dict[str, Any]:
    """Compact identity block for a symbol, shared across user prompts."""
    return {
        "ticker": symbol.ticker,
        "name": symbol.name,
        "exchange": symbol.exchange,
        "currency": symbol.currency,
        "asset_type": symbol.asset_type,
    }


# --------------------------------------------------------------------------- #
# BaseAgent
# --------------------------------------------------------------------------- #


class BaseAgent:
    """Template shared by every actor in the pipeline.

    The analyst agents use the full triad below unchanged (only overriding
    ``_system_body``, ``build_user_prompt`` and extending ``validate_output``).
    The synthesizer and validator override ``run``/``build_user_prompt`` because
    they consume aggregated inputs rather than a raw :class:`AgentContext`, but
    they still reuse ``build_system_prompt`` (so the lessons block is applied),
    the coercion helpers and ``AgentResult``.
    """

    #: Stable identifier persisted as ``analyses.agent_name`` — override.
    name: str = "base"

    #: Sampling parameters passed to ``LLMClient.complete_json``.
    temperature: float = 0.2
    max_tokens: int = 1500

    # -- prompt construction ------------------------------------------------ #

    def _system_body(self) -> str:
        """Return the agent-specific instruction body (without the lessons block)."""
        raise NotImplementedError

    def build_system_prompt(self, lessons: list[str]) -> str:
        """Compose the full system prompt for any actor.

        The shared plain-Italian style block is injected here (before the lessons
        block) so EVERY actor's user-facing ``*_it`` output automatically uses
        correct-but-explained financial terminology; the prompt always ends with
        the lessons block so no subclass can forget it.
        """
        return (
            f"{self._system_body()}\n\n{PLAIN_ITALIAN_STYLE}"
            f"\n\n{format_lessons_block(lessons)}"
        )

    def build_user_prompt(self, ctx: AgentContext) -> str:
        """Serialise the slice of ``ctx`` this agent reasons about (analysts)."""
        raise NotImplementedError

    # -- execution ---------------------------------------------------------- #

    async def run(self, ctx: AgentContext, llm: LLMClient) -> AgentResult:
        """Run one analyst agent end-to-end.

        Exceptions raised by the LLM layer (``LLMUnavailableError``,
        ``LLMOutputError``, ``ProviderError``) intentionally propagate so the
        orchestrator's ``asyncio.gather(..., return_exceptions=True)`` can mark
        this agent as FAILED without aborting its siblings.
        """
        system = self.build_system_prompt(ctx.lessons)
        user = self.build_user_prompt(ctx)
        pref_provider, pref_model = resolve_llm_pref(self.name)
        parsed, provider = await llm.complete_json(
            system,
            user,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            provider=pref_provider,
            model=pref_model,
        )
        output = self.validate_output(parsed)
        return AgentResult(agent_name=self.name, output=output, provider=provider)

    # -- output validation -------------------------------------------------- #

    def validate_output(self, data: dict) -> dict:
        """Coerce raw LLM output into the common analyst schema (blueprint 5.3).

        Clamps ``signal`` to [-1, 1] and ``confidence`` to [0, 1], enforces the
        ``stance``/``data_quality`` enums with conservative defaults, guarantees
        ``summary_it`` (fallback empty string) and truncates ``key_points`` to 5
        and ``risks`` to 3. Analyst subclasses call ``super().validate_output``
        and then attach their one extra field.
        """
        raw = data if isinstance(data, dict) else {}
        return {
            "stance": coerce_enum(raw.get("stance"), STANCE_VALUES, DEFAULT_STANCE),
            "signal": clamp(coerce_float(raw.get("signal"), 0.0), -1.0, 1.0),
            "confidence": clamp(coerce_float(raw.get("confidence"), 0.0), 0.0, 1.0),
            "key_points": coerce_str_list(raw.get("key_points"), MAX_KEY_POINTS),
            "risks": coerce_str_list(raw.get("risks"), MAX_RISKS),
            "data_quality": coerce_enum(
                raw.get("data_quality"), DATA_QUALITY_VALUES, DEFAULT_DATA_QUALITY
            ),
            "summary_it": coerce_str(raw.get("summary_it"), ""),
        }
