"""LLM provider adapters over httpx (blueprint 5.1).

Two providers with one tiny common interface: send a (system, user) pair,
get raw text back. Everything JSON-related happens one layer up in
``LLMClient``; everything HTTP-related is contained here.
"""

from __future__ import annotations

import httpx

REQUEST_TIMEOUT_S = 60.0


class ProviderError(Exception):
    """A provider call failed (network error, non-2xx, malformed envelope).

    ``retryable`` marks failures worth retrying on the same provider
    (429/5xx/timeouts); auth or request errors (4xx) are not. For rate limits
    ``retry_after`` carries the wait (seconds) the provider asked for, when it
    said so (Gemini RetryInfo / Retry-After header).
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after


def parse_retry_after(response: httpx.Response) -> float | None:
    """Extract the provider-requested retry delay (seconds) from a 429 response.

    Checks the standard ``Retry-After`` header first, then Gemini's
    ``error.details[].retryDelay`` (a string like ``"26s"``). ``None`` when the
    provider did not say.
    """
    header = response.headers.get("retry-after")
    if header:
        try:
            return max(0.0, float(header))
        except ValueError:
            pass
    try:
        data = response.json()
        details = data["error"]["details"]
    except Exception:
        return None
    for item in details if isinstance(details, list) else []:
        delay = item.get("retryDelay") if isinstance(item, dict) else None
        if isinstance(delay, str) and delay.endswith("s"):
            try:
                return max(0.0, float(delay[:-1]))
            except ValueError:
                continue
    return None


def _classify_status(status: int) -> bool:
    """Whether an HTTP status is worth a same-provider retry."""
    return status == 429 or status >= 500


class BaseProvider:
    """Minimal chat-completion interface shared by all providers."""

    name: str = "base"

    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model
        self._client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S)
        # (prompt_tokens, completion_tokens) of the most recent successful chat,
        # or None when the provider did not report usage. Set synchronously right
        # before ``chat`` returns, so ``LLMClient`` can read it immediately after
        # the await with no interleaving coroutine (used only for cost logging).
        self.last_usage: tuple[int | None, int | None] | None = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key.strip())

    async def chat(
        self,
        system: str,
        user: str,
        temperature: float,
        max_tokens: int,
        model: str | None = None,
    ) -> str:
        raise NotImplementedError

    async def _post(self, url: str, *, headers: dict | None = None, json: dict) -> dict:
        try:
            response = await self._client.post(url, headers=headers, json=json)
        except httpx.TimeoutException as exc:
            raise ProviderError(f"{self.name}: timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name}: network error: {exc}", retryable=True) from exc

        if response.status_code == 429:
            raise ProviderError(
                f"{self.name}: HTTP 429 (limite di richieste o quota del provider superati)",
                status=429,
                retryable=True,
                retry_after=parse_retry_after(response),
            )
        if response.status_code >= 400:
            raise ProviderError(
                f"{self.name}: HTTP {response.status_code}: {response.text[:300]}",
                status=response.status_code,
                retryable=_classify_status(response.status_code),
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError(f"{self.name}: non-JSON response envelope", retryable=True) from exc

    async def aclose(self) -> None:
        await self._client.aclose()


class OpenRouterProvider(BaseProvider):
    """OpenAI-compatible chat completions on openrouter.ai."""

    name = "openrouter"
    _URL = "https://openrouter.ai/api/v1/chat/completions"

    async def chat(
        self,
        system: str,
        user: str,
        temperature: float,
        max_tokens: int,
        model: str | None = None,
    ) -> str:
        if not self.configured:
            raise ProviderError("openrouter: OPENROUTER_API_KEY non configurata")
        payload = {
            "model": model or self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": "http://localhost",
            "X-Title": "trade-max",
        }
        data = await self._post(self._URL, headers=headers, json=payload)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                f"openrouter: unexpected response shape: {str(data)[:300]}", retryable=True
            ) from exc
        if not isinstance(content, str) or not content.strip():
            raise ProviderError("openrouter: empty completion content", retryable=True)
        usage = data.get("usage") if isinstance(data, dict) else None
        self.last_usage = (
            (usage.get("prompt_tokens"), usage.get("completion_tokens"))
            if isinstance(usage, dict)
            else None
        )
        return content


class GeminiProvider(BaseProvider):
    """Google Gemini generateContent REST API."""

    name = "gemini"
    _URL_TEMPLATE = (
        "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    )

    async def chat(
        self,
        system: str,
        user: str,
        temperature: float,
        max_tokens: int,
        model: str | None = None,
    ) -> str:
        if not self.configured:
            raise ProviderError("gemini: GEMINI_API_KEY non configurata")
        effective_model = model or self.model
        if "gemma" in effective_model.lower():
            # Gemma models on the Gemini API support neither systemInstruction
            # nor JSON mode (responseMimeType): fold the system prompt into the
            # user turn and rely on the prompt's strict-JSON instruction plus
            # LLMClient's corrective retry.
            payload = {
                "contents": [
                    {"role": "user", "parts": [{"text": f"{system}\n\n---\n\n{user}"}]}
                ],
                "generationConfig": {
                    "temperature": temperature,
                    "maxOutputTokens": max_tokens,
                },
            }
        else:
            payload = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {
                    "temperature": temperature,
                    "maxOutputTokens": max_tokens,
                    "responseMimeType": "application/json",
                },
            }
        url = self._URL_TEMPLATE.format(model=effective_model)
        # The key travels in a header, not the URL, so it can never leak in logs.
        headers = {"x-goog-api-key": self.api_key}
        data = await self._post(url, headers=headers, json=payload)
        try:
            parts = data["candidates"][0]["content"]["parts"]
            content = "".join(part.get("text", "") for part in parts)
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                f"gemini: unexpected response shape: {str(data)[:300]}", retryable=True
            ) from exc
        if not content.strip():
            raise ProviderError("gemini: empty completion content", retryable=True)
        usage = data.get("usageMetadata") if isinstance(data, dict) else None
        self.last_usage = (
            (usage.get("promptTokenCount"), usage.get("candidatesTokenCount"))
            if isinstance(usage, dict)
            else None
        )
        return content
