"""LLMClient: JSON completions with per-provider retry and provider fallback."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from app.llm.json_utils import LLMOutputError, extract_json
from app.llm.providers import (
    BaseProvider,
    GeminiProvider,
    OllamaProvider,
    OpenRouterProvider,
    ProviderError,
)

if TYPE_CHECKING:
    from app.llm.runtime import EffectiveLlmConfig

logger = logging.getLogger(__name__)

ATTEMPTS_PER_PROVIDER = 2
#: Rate-limited calls (HTTP 429) get one extra try: with the provider-declared
#: retry delay honoured, a third attempt usually lands.
RATE_LIMIT_ATTEMPTS = 3
RETRY_BACKOFF_S = 2.0
#: Never wait longer than this for a provider-declared retry delay.
MAX_RETRY_AFTER_S = 65.0

#: App-wide cap on concurrent LLM calls: free-tier providers throttle bursts
#: (4 analysts in parallel × retries was enough to trip Gemini's free RPM).
LLM_MAX_CONCURRENCY = 2

# Monkeypatchable in tests (avoids real waits).
_sleep = asyncio.sleep

_semaphore: asyncio.Semaphore | None = None
_semaphore_loop: asyncio.AbstractEventLoop | None = None


def _get_semaphore() -> asyncio.Semaphore:
    """Per-event-loop global semaphore bounding concurrent LLM calls.

    Recreated when the running loop changes (tests spin up many loops); in the
    single-loop production process it is effectively a module singleton.
    """
    global _semaphore, _semaphore_loop
    loop = asyncio.get_running_loop()
    if _semaphore is None or _semaphore_loop is not loop:
        _semaphore = asyncio.Semaphore(LLM_MAX_CONCURRENCY)
        _semaphore_loop = loop
    return _semaphore

#: Appended to the user prompt after a malformed-JSON reply: small models
#: (e.g. Gemma, which has no JSON mode) often answer with markdown prose on the
#: first try but comply once told off explicitly.
JSON_RETRY_SUFFIX = (
    "\n\nIMPORTANT: your previous reply was NOT valid JSON. Respond with ONLY the "
    "JSON object described in the instructions, starting with '{' and ending with "
    "'}' - no prose, no markdown, no bullet points, no code fences."
)


class LLMUnavailableError(Exception):
    """Every configured provider failed; the pipeline cannot proceed."""


class LLMClient:
    """Provider-agnostic JSON completion client.

    Providers are tried in order (primary first, then the fallback when fallback
    is enabled); each gets :data:`ATTEMPTS_PER_PROVIDER` tries. Malformed JSON
    output counts as a retryable failure — a second sample or the other provider
    often fixes it.

    The client is built from the *effective* LLM config
    (:class:`app.llm.runtime.EffectiveLlmConfig`, env with DB overrides) and is a
    process-wide singleton. Rather than rebuilding on every config change, it
    re-syncs its providers **in place** at the top of :meth:`complete_json`
    whenever the runtime *generation* has advanced (option A of the design): the
    pooled ``httpx`` clients are reused (keys/models travel per-request, not in
    the connection), so a config change never churns connections.
    """

    def __init__(self, config: "EffectiveLlmConfig | None" = None):
        # Provider shells; api_key/base_url/model are (re)assigned by ``_apply_config``.
        self._openrouter = OpenRouterProvider("", "")
        self._gemini = GeminiProvider("", "")
        self._ollama = OllamaProvider("", "")
        # Insertion order doubles as the canonical fallback order for providers
        # other than the primary.
        self._by_name: dict[str, BaseProvider] = {
            self._openrouter.name: self._openrouter,
            self._gemini.name: self._gemini,
            self._ollama.name: self._ollama,
        }
        self._fallback_enabled = True
        self.providers: list[BaseProvider] = [self._openrouter]
        self._generation = -1
        if config is not None:
            from app.llm.runtime import get_generation

            self._apply_config(config)
            self._generation = get_generation()
        else:
            # Read generation-then-config atomically so the pairing is never stale.
            self._sync_config()

    def _apply_config(self, config: "EffectiveLlmConfig") -> None:
        """Update provider keys/URL/models and the primary→fallback ordering in place."""
        self._openrouter.api_key = config.openrouter_api_key
        self._openrouter.model = config.openrouter_model
        self._gemini.api_key = config.gemini_api_key
        self._gemini.model = config.gemini_model
        self._ollama.base_url = config.ollama_base_url
        self._ollama.model = config.ollama_model
        self._fallback_enabled = config.fallback_enabled
        # Effective primary first (default to openrouter for an unknown value),
        # then every OTHER configured provider in canonical order when fallback is
        # enabled. The primary is always kept at the head even if unconfigured —
        # ``complete_json`` skips unconfigured providers with a clear error.
        primary = self._by_name.get(config.primary_provider, self._openrouter)
        self.providers = [primary]
        if config.fallback_enabled:
            for prov in self._by_name.values():
                if prov is not primary and prov.configured:
                    self.providers.append(prov)

    def _sync_config(self) -> None:
        """Re-sync providers from the effective config when the generation changed.

        The generation is read *before* the config so the cached config is always
        at least as fresh as the recorded generation; over-syncing (recomputing
        one extra time) is harmless, under-syncing (stale keys) cannot happen.
        """
        from app.llm.runtime import get_effective, get_generation

        gen = get_generation()
        if gen == self._generation:
            return
        self._apply_config(get_effective())
        self._generation = gen

    def _resolve_order(
        self, provider: str | None, model: str | None
    ) -> list[tuple[BaseProvider, str | None]]:
        """Ordered ``(provider, model_override)`` attempts for one call.

        With no per-call ``provider``, this is the env-configured default ordering
        (primary, then every other configured provider when fallback is enabled)
        with no model override. When ``provider`` names a known provider, it is
        tried FIRST with ``model`` overriding its default model for this call; then,
        when fallback is enabled, the effective primary and every other configured
        provider follow (each with its own default model). Duplicates are removed
        while preserving order.
        """
        if provider is not None and provider in self._by_name:
            preferred = self._by_name[provider]
            order: list[tuple[BaseProvider, str | None]] = [(preferred, model)]
            if self._fallback_enabled:
                # ``self.providers`` is already "primary, then other configured
                # providers"; drop the preferred one (tried first above).
                for prov in self.providers:
                    if prov is not preferred and prov.configured:
                        order.append((prov, None))
            return order
        return [(prov, None) for prov in self.providers]

    async def complete_json(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 2500,
        provider: str | None = None,
        model: str | None = None,
    ) -> tuple[dict, str]:
        """Return ``(parsed_json, provider_name)`` or raise :class:`LLMUnavailableError`.

        When ``provider`` is given (and known), that provider is tried first with
        ``model`` overriding its default model for this call; the other configured
        provider is used as fallback with its own default model. Existing callers
        that pass neither keep the env-configured ordering and models unchanged.
        """
        # Pick up any provider/key/model change saved from the app's Settings page.
        self._sync_config()
        errors: list[str] = []
        corrective = False
        for prov, model_override in self._resolve_order(provider, model):
            if not prov.configured:
                errors.append(f"{prov.name}: chiave API non configurata")
                continue
            attempt = 0
            max_attempts = ATTEMPTS_PER_PROVIDER
            while attempt < max_attempts:
                attempt += 1
                user_payload = user + JSON_RETRY_SUFFIX if corrective else user
                wait_s = RETRY_BACKOFF_S * attempt
                try:
                    # The semaphore bounds bursts app-wide (parallel analysts ×
                    # retries trip free-tier rate limits); it is held only for
                    # the network call, never while sleeping.
                    async with _get_semaphore():
                        text = await prov.chat(
                            system, user_payload, temperature, max_tokens, model=model_override
                        )
                    in_tokens, out_tokens = prov.last_usage or (None, None)
                    logger.info(
                        "LLM call ok: provider=%s model=%s prompt_tokens=%s completion_tokens=%s",
                        prov.name,
                        model_override or prov.model,
                        in_tokens,
                        out_tokens,
                    )
                    return extract_json(text), prov.name
                except LLMOutputError as exc:
                    errors.append(f"{prov.name} (tentativo {attempt}): {exc}")
                    logger.warning("Malformed JSON from %s (attempt %d): %s", prov.name, attempt, exc)
                    # The next attempt (same or fallback provider) gets an
                    # explicit only-JSON reminder appended to the user prompt.
                    corrective = True
                except ProviderError as exc:
                    errors.append(f"{prov.name} (tentativo {attempt}): {exc}")
                    logger.warning("Provider %s failed (attempt %d): %s", prov.name, attempt, exc)
                    if not exc.retryable:
                        break  # auth/request error: same provider won't recover
                    if exc.status == 429:
                        # Rate limited: honour the provider-declared delay and
                        # grant the extra attempt.
                        max_attempts = max(max_attempts, RATE_LIMIT_ATTEMPTS)
                        if exc.retry_after is not None:
                            wait_s = min(exc.retry_after + 1.0, MAX_RETRY_AFTER_S)
                if attempt < max_attempts:
                    await _sleep(wait_s)
        raise LLMUnavailableError(
            "Nessun provider LLM disponibile: " + " | ".join(errors[-4:])
        )

    async def aclose(self) -> None:
        for provider in self._by_name.values():
            await provider.aclose()
