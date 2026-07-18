"""Robust extraction of JSON objects from raw LLM text output."""

from __future__ import annotations

import json
import re


class LLMOutputError(Exception):
    """The model returned text from which no JSON object could be recovered."""


_CODE_FENCE_RE = re.compile(r"```(?:json|javascript|js)?\s*", re.IGNORECASE)
# A comma followed only by whitespace and a closing brace/bracket is invalid
# JSON but a frequent LLM slip; stripping it is always safe.
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def extract_json(text: str) -> dict:
    """Parse the first JSON object found in ``text``.

    Strategy: strip code fences, slice from the first ``{`` to the last ``}``,
    ``json.loads``; on failure retry once with trailing commas repaired.
    Raises :class:`LLMOutputError` when nothing parseable remains or the
    parsed value is not an object.
    """
    if not isinstance(text, str) or not text.strip():
        raise LLMOutputError("Empty LLM response")

    cleaned = _CODE_FENCE_RE.sub("", text).replace("```", "")
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise LLMOutputError(f"No JSON object found in LLM response: {text[:200]!r}")

    candidate = cleaned[start : end + 1]
    for attempt in (candidate, _TRAILING_COMMA_RE.sub(r"\1", candidate)):
        try:
            parsed = json.loads(attempt)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
        raise LLMOutputError(f"LLM returned JSON of type {type(parsed).__name__}, expected object")

    raise LLMOutputError(f"Unparseable JSON in LLM response: {candidate[:200]!r}")
