"""Gemma-model compatibility and corrective-JSON-retry behaviour.

Gemma models on the Gemini API support neither ``systemInstruction`` nor JSON
mode (``responseMimeType``): the provider must fold the system prompt into the
user turn and drop the mime type. And when any model replies with prose instead
of JSON, the client's next attempt must carry an explicit only-JSON reminder.
"""

from __future__ import annotations

import pytest

import app.llm.client as client_module
from app.llm.client import JSON_RETRY_SUFFIX, LLMClient
from app.llm.providers import GeminiProvider


async def _capture_post(monkeypatch: pytest.MonkeyPatch) -> dict:
    captured: dict = {}

    async def fake_post(self, url: str, *, headers=None, json: dict) -> dict:  # noqa: ANN001
        captured["url"] = url
        captured["payload"] = json
        return {"candidates": [{"content": {"parts": [{"text": '{"a": 1}'}]}}]}

    monkeypatch.setattr(GeminiProvider, "_post", fake_post)
    return captured


async def test_gemma_payload_has_no_system_instruction_nor_json_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = await _capture_post(monkeypatch)
    provider = GeminiProvider("test-key", "gemma-3-27b-it")

    await provider.chat("SYSTEM PROMPT", "USER PROMPT", 0.2, 100)

    payload = captured["payload"]
    assert "systemInstruction" not in payload
    assert "responseMimeType" not in payload["generationConfig"]
    merged = payload["contents"][0]["parts"][0]["text"]
    assert "SYSTEM PROMPT" in merged and "USER PROMPT" in merged
    assert "gemma-3-27b-it" in captured["url"]


async def test_gemini_payload_keeps_system_instruction_and_json_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = await _capture_post(monkeypatch)
    provider = GeminiProvider("test-key", "gemini-2.0-flash")

    await provider.chat("SYSTEM PROMPT", "USER PROMPT", 0.2, 100)

    payload = captured["payload"]
    assert payload["systemInstruction"]["parts"][0]["text"] == "SYSTEM PROMPT"
    assert payload["generationConfig"]["responseMimeType"] == "application/json"


async def test_gemma_model_override_per_call_switches_payload_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = await _capture_post(monkeypatch)
    provider = GeminiProvider("test-key", "gemini-2.0-flash")

    await provider.chat("SYS", "USER", 0.2, 100, model="gemma-3-4b-it")

    assert "systemInstruction" not in captured["payload"]
    assert "gemma-3-4b-it" in captured["url"]


class _ProseThenJsonProvider:
    """Fake provider: prose on the first call, valid JSON afterwards."""

    name = "gemini"
    model = "gemma-3-27b-it"
    configured = True
    last_usage = None

    def __init__(self) -> None:
        self.user_prompts: list[str] = []

    async def chat(self, system, user, temperature, max_tokens, model=None):  # noqa: ANN001
        self.user_prompts.append(user)
        if len(self.user_prompts) == 1:
            return "*   Ticker: AAPL\n*   Just some markdown bullets, no JSON."
        return '{"stance": "NEUTRAL"}'


async def test_corrective_retry_appends_only_json_reminder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module, "RETRY_BACKOFF_S", 0.0)
    client = LLMClient()
    monkeypatch.setattr(client, "_sync_config", lambda: None)
    fake = _ProseThenJsonProvider()
    client.providers = [fake]

    parsed, provider_name = await client.complete_json("SYS", "USER PROMPT")

    assert parsed == {"stance": "NEUTRAL"}
    assert provider_name == "gemini"
    assert len(fake.user_prompts) == 2
    assert JSON_RETRY_SUFFIX not in fake.user_prompts[0]
    assert fake.user_prompts[1].endswith(JSON_RETRY_SUFFIX)
    assert fake.user_prompts[1].startswith("USER PROMPT")
