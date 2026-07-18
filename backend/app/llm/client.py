"""LLMClient: JSON completions with per-provider retry and provider fallback."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from app.llm.json_utils import LLMOutputError, extract_json
from app.llm.providers import BaseProvider, GeminiProvider, OpenRouterProvider, ProviderError

if TYPE_CHECKING:
    from app.llm.runtime import EffectiveLlmConfig

logger = logging.getLogger(__name__)

ATTEMPTS_PER_PROVIDER = 2
RETRY_BACKOFF_S = 2.0


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
        # Provider shells; api_key/model are (re)assigned by ``_apply_config``.
        self._openrouter = OpenRouterProvider("", "")
        self._gemini = GeminiProvider("", "")
        self._by_name: dict[str, BaseProvider] = {
            self._openrouter.name: self._openrouter,
            self._gemini.name: self._gemini,
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
        """Update provider keys/models and the primary→fallback ordering in place."""
        self._openrouter.api_key = config.openrouter_api_key
        self._openrouter.model = config.openrouter_model
        self._gemini.api_key = config.gemini_api_key
        self._gemini.model = config.gemini_model
        self._fallback_enabled = config.fallback_enabled
        ordered = (
            [self._gemini, self._openrouter]
            if config.primary_provider == "gemini"
            else [self._openrouter, self._gemini]
        )
        primary, fallback = ordered
        self.providers = [primary]
        if config.fallback_enabled and fallback.configured:
            self.providers.append(fallback)

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
        (primary, then fallback when enabled) with no model override. When
        ``provider`` names a known provider, it is tried FIRST with ``model``
        overriding its default model for this call, and the other provider is
        appended as a fallback (its own default model) subject to the same
        fallback/configured rules as the default ordering.
        """
        if provider is not None and provider in self._by_name:
            preferred = self._by_name[provider]
            order: list[tuple[BaseProvider, str | None]] = [(preferred, model)]
            if self._fallback_enabled:
                other = self._gemini if preferred is self._openrouter else self._openrouter
                if other.configured:
                    order.append((other, None))
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
        for prov, model_override in self._resolve_order(provider, model):
            if not prov.configured:
                errors.append(f"{prov.name}: chiave API non configurata")
                continue
            for attempt in range(1, ATTEMPTS_PER_PROVIDER + 1):
                try:
                    text = await prov.chat(
                        system, user, temperature, max_tokens, model=model_override
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
                except ProviderError as exc:
                    errors.append(f"{prov.name} (tentativo {attempt}): {exc}")
                    logger.warning("Provider %s failed (attempt %d): %s", prov.name, attempt, exc)
                    if not exc.retryable:
                        break  # auth/request error: same provider won't recover
                if attempt < ATTEMPTS_PER_PROVIDER:
                    await asyncio.sleep(RETRY_BACKOFF_S * attempt)
        raise LLMUnavailableError(
            "Nessun provider LLM disponibile: " + " | ".join(errors[-4:])
        )

    async def aclose(self) -> None:
        for provider in self._by_name.values():
            await provider.aclose()
