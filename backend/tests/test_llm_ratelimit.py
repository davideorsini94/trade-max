"""Rate-limit (HTTP 429) handling: retry_after parsing, extra attempt, waits."""

from __future__ import annotations

import httpx
import pytest

import app.llm.client as client_module
from app.llm.client import LLMClient, MAX_RETRY_AFTER_S
from app.llm.providers import ProviderError, parse_retry_after


def _response(status: int, *, headers: dict | None = None, json_body: dict | None = None) -> httpx.Response:
    request = httpx.Request("POST", "https://example.test/")
    return httpx.Response(status, request=request, headers=headers or {}, json=json_body or {})


def test_parse_retry_after_header() -> None:
    assert parse_retry_after(_response(429, headers={"Retry-After": "12"})) == 12.0


def test_parse_retry_after_gemini_retry_info() -> None:
    body = {
        "error": {
            "code": 429,
            "details": [
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "26s"},
            ],
        }
    }
    assert parse_retry_after(_response(429, json_body=body)) == 26.0


def test_parse_retry_after_absent() -> None:
    assert parse_retry_after(_response(429, json_body={"error": {"code": 429}})) is None


class _RateLimitedTwiceProvider:
    """Fake provider: two 429s (with declared delays), then valid JSON."""

    name = "gemini"
    model = "gemma-3-27b-it"
    configured = True
    last_usage = None

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, system, user, temperature, max_tokens, model=None):  # noqa: ANN001
        self.calls += 1
        if self.calls <= 2:
            raise ProviderError(
                "gemini: HTTP 429 (limite di richieste o quota del provider superati)",
                status=429,
                retryable=True,
                retry_after=7.0,
            )
        return '{"ok": true}'


async def test_429_gets_extra_attempt_and_honours_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(client_module, "_sleep", fake_sleep)
    client = LLMClient()
    monkeypatch.setattr(client, "_sync_config", lambda: None)
    fake = _RateLimitedTwiceProvider()
    client.providers = [fake]

    parsed, provider_name = await client.complete_json("SYS", "USER")

    # Third attempt succeeded (default is 2: the 429 path granted the extra one).
    assert parsed == {"ok": True}
    assert provider_name == "gemini"
    assert fake.calls == 3
    # Both waits used the provider-declared delay (+1s), not the blind backoff.
    assert sleeps == [8.0, 8.0]


async def test_retry_after_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(client_module, "_sleep", fake_sleep)

    class HugeDelayProvider(_RateLimitedTwiceProvider):
        async def chat(self, system, user, temperature, max_tokens, model=None):  # noqa: ANN001
            self.calls += 1
            if self.calls == 1:
                raise ProviderError("429", status=429, retryable=True, retry_after=600.0)
            return '{"ok": true}'

    client = LLMClient()
    monkeypatch.setattr(client, "_sync_config", lambda: None)
    client.providers = [HugeDelayProvider()]

    parsed, _ = await client.complete_json("SYS", "USER")

    assert parsed == {"ok": True}
    assert sleeps == [MAX_RETRY_AFTER_S]
