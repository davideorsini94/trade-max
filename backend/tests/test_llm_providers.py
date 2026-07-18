"""Offline tests for the DB-backed LLM provider settings (blueprint: Settings page).

Covers ``GET``/``PUT`` ``/api/llm/providers``, ``POST /api/llm/providers/test``,
the effective-config invalidation seen by the ``LLMClient``, and the ``/api/health``
provider status.

Everything is fully offline: ``app.llm.runtime`` is redirected to the per-test
temp SQLite DB (the same one the ``client`` fixture writes to, via ``session_scope``)
and to a fake env ``Settings``; upstream provider fetches are monkeypatched. No
network, no real ``.env`` / ``trademax.db``.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

import app.api.llm as llm_api
import app.llm.runtime as runtime
from app.llm.client import LLMClient
from app.schemas import LlmModelInfo


def _env(**overrides: object) -> SimpleNamespace:
    """A Settings-shaped stand-in for the env layer that ``runtime`` reads."""
    values: dict[str, object] = {
        "openrouter_api_key": "",
        "gemini_api_key": "",
        "llm_provider": "openrouter",
        "llm_fallback_enabled": True,
        "openrouter_model": "openai/gpt-4o-mini",
        "gemini_model": "gemini-2.0-flash",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture(autouse=True)
def _provider_runtime(
    monkeypatch: pytest.MonkeyPatch, db_session_factory: sessionmaker[Session]
) -> None:
    """Point ``runtime`` at the per-test temp DB + a no-keys env, and reset caches.

    The ``client`` fixture and this fixture share the SAME ``db_session_factory``
    (fixtures are cached per test), so ``runtime.get_effective`` reads exactly the
    rows the endpoints write.
    """

    @contextmanager
    def _scope():
        db = db_session_factory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    monkeypatch.setattr(runtime, "session_scope", _scope)
    monkeypatch.setattr(runtime, "get_settings", lambda: _env())
    runtime.invalidate_effective()
    llm_api._MODELS_CACHE.clear()
    yield
    runtime.invalidate_effective()
    llm_api._MODELS_CACHE.clear()


def _set_env(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    """Override the fake env config and invalidate the effective-config cache."""
    monkeypatch.setattr(runtime, "get_settings", lambda: _env(**overrides))
    runtime.invalidate_effective()


# --------------------------------------------------------------------------- #
# GET /api/llm/providers
# --------------------------------------------------------------------------- #


def test_get_providers_defaults_env_only(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_env(monkeypatch, openrouter_api_key="sk-or-abcdef1234567890")

    body = client.get("/api/llm/providers").json()

    assert body["primary_provider"] == "openrouter"
    assert body["fallback_enabled"] is True

    providers = {p["provider"]: p for p in body["providers"]}
    # OpenRouter comes only from .env -> source "env", masked (never the full key).
    assert providers["openrouter"]["configured"] is True
    assert providers["openrouter"]["source"] == "env"
    assert providers["openrouter"]["key_masked"] == "sk-or…7890"
    assert "abcdef" not in providers["openrouter"]["key_masked"]
    assert providers["openrouter"]["default_model"] == "openai/gpt-4o-mini"
    # Gemini absent everywhere -> source null, no mask.
    assert providers["gemini"]["configured"] is False
    assert providers["gemini"]["source"] is None
    assert providers["gemini"]["key_masked"] is None
    assert providers["gemini"]["default_model"] == "gemini-2.0-flash"


def test_get_providers_masks_short_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_env(monkeypatch, gemini_api_key="abcd1234")  # short (< 10 chars)
    providers = {p["provider"]: p for p in client.get("/api/llm/providers").json()["providers"]}
    # Short keys use the "…"+last4 form (no first-5 prefix that would reveal it).
    assert providers["gemini"]["key_masked"] == "…1234"


# --------------------------------------------------------------------------- #
# PUT /api/llm/providers
# --------------------------------------------------------------------------- #


def test_put_sets_key_source_app_and_unlocks_models(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Env has no OpenRouter key: initially not configured.
    assert client.get("/api/llm/providers").json()["providers"][0]["configured"] is False

    resp = client.put("/api/llm/providers", json={"openrouter_api_key": "sk-or-appkey-987654321"})
    assert resp.status_code == 200
    openrouter = {p["provider"]: p for p in resp.json()["providers"]}["openrouter"]
    assert openrouter["configured"] is True
    assert openrouter["source"] == "app"
    assert openrouter["key_masked"] == "sk-or…4321"
    assert "appkey" not in openrouter["key_masked"]

    # The app-stored key immediately unlocks the models listing for that provider.
    async def fake_fetch(api_key: str) -> list[LlmModelInfo]:
        assert api_key == "sk-or-appkey-987654321"  # the effective (app) key
        return [LlmModelInfo(id="openai/gpt-4o", label="GPT-4o")]

    monkeypatch.setattr(llm_api, "_fetch_openrouter_models", fake_fetch)
    models = client.get("/api/llm/models", params={"provider": "openrouter"})
    assert models.status_code == 200
    assert models.json()["models"] == [{"id": "openai/gpt-4o", "label": "GPT-4o"}]


def test_put_null_deletes_key_and_falls_back_to_env(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_env(monkeypatch, openrouter_api_key="env-openrouter-key-1234567890")

    # Store an app key: it wins over env (source "app").
    stored = client.put(
        "/api/llm/providers", json={"openrouter_api_key": "app-openrouter-key-0987654321"}
    ).json()
    assert stored["providers"][0]["source"] == "app"

    # null deletes the stored key; the env key remains as fallback (source "env").
    reset = client.put("/api/llm/providers", json={"openrouter_api_key": None}).json()
    openrouter = {p["provider"]: p for p in reset["providers"]}["openrouter"]
    assert openrouter["configured"] is True
    assert openrouter["source"] == "env"
    assert openrouter["key_masked"] == "env-o…7890"


def test_put_absent_field_leaves_key_unchanged(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.put("/api/llm/providers", json={"openrouter_api_key": "sk-or-keepme-111122223"})
    # A PUT that omits the key fields must not touch the stored key.
    body = client.put("/api/llm/providers", json={"fallback_enabled": False}).json()
    openrouter = {p["provider"]: p for p in body["providers"]}["openrouter"]
    assert openrouter["configured"] is True
    assert openrouter["source"] == "app"
    assert body["fallback_enabled"] is False


def test_put_primary_provider_and_fallback_roundtrip(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    resp = client.put(
        "/api/llm/providers", json={"primary_provider": "gemini", "fallback_enabled": False}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["primary_provider"] == "gemini"
    assert body["fallback_enabled"] is False

    # A fresh GET reflects the persisted state.
    reread = client.get("/api/llm/providers").json()
    assert reread["primary_provider"] == "gemini"
    assert reread["fallback_enabled"] is False


def test_put_primary_provider_null_resets_to_env(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_env(monkeypatch, llm_provider="openrouter")
    client.put("/api/llm/providers", json={"primary_provider": "gemini"})
    assert client.get("/api/llm/providers").json()["primary_provider"] == "gemini"
    # null clears the override -> back to the env-configured provider.
    body = client.put("/api/llm/providers", json={"primary_provider": None}).json()
    assert body["primary_provider"] == "openrouter"


def test_put_unknown_primary_provider_returns_400(client: TestClient) -> None:
    resp = client.put("/api/llm/providers", json={"primary_provider": "cohere"})
    assert resp.status_code == 400
    assert "Provider non valido" in resp.json()["detail"]


def test_put_empty_key_returns_400(client: TestClient) -> None:
    resp = client.put("/api/llm/providers", json={"gemini_api_key": "   "})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Chiave API non valida."


def test_put_trims_stored_key(client: TestClient) -> None:
    body = client.put(
        "/api/llm/providers", json={"gemini_api_key": "  gm-secret-key-4242424242  "}
    ).json()
    gemini = {p["provider"]: p for p in body["providers"]}["gemini"]
    assert gemini["configured"] is True
    assert gemini["source"] == "app"
    # Masked form of the TRIMMED key.
    assert gemini["key_masked"] == "gm-se…4242"


# --------------------------------------------------------------------------- #
# POST /api/llm/providers/test
# --------------------------------------------------------------------------- #


def test_test_endpoint_ok(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    client.put("/api/llm/providers", json={"openrouter_api_key": "sk-or-testkey-55667788"})

    async def fake_check(api_key: str) -> None:
        assert api_key == "sk-or-testkey-55667788"

    monkeypatch.setattr(llm_api, "_check_openrouter_key", fake_check)
    resp = client.post("/api/llm/providers/test", json={"provider": "openrouter"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "riuscita" in body["detail_it"].lower()


def test_test_endpoint_auth_failure_returns_ok_false(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.put("/api/llm/providers", json={"gemini_api_key": "gm-badkey-99887766"})

    async def boom(api_key: str) -> list[LlmModelInfo]:
        request = httpx.Request("GET", "https://generativelanguage.googleapis.com/")
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError("unauthorized", request=request, response=response)

    monkeypatch.setattr(llm_api, "_fetch_gemini_models", boom)
    resp = client.post("/api/llm/providers/test", json={"provider": "gemini"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "non valida" in body["detail_it"].lower()


def test_test_endpoint_network_failure_returns_ok_false(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.put("/api/llm/providers", json={"openrouter_api_key": "sk-or-netkey-12121212"})

    async def boom(api_key: str) -> None:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(llm_api, "_check_openrouter_key", boom)
    resp = client.post("/api/llm/providers/test", json={"provider": "openrouter"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "impossibile contattare" in body["detail_it"].lower()


def test_test_endpoint_no_key_returns_400(client: TestClient) -> None:
    resp = client.post("/api/llm/providers/test", json={"provider": "gemini"})
    assert resp.status_code == 400
    assert "non configurato" in resp.json()["detail"]


def test_test_endpoint_unknown_provider_returns_400(client: TestClient) -> None:
    resp = client.post("/api/llm/providers/test", json={"provider": "cohere"})
    assert resp.status_code == 400
    assert "Provider non valido" in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# Effective-config invalidation seen by the LLMClient
# --------------------------------------------------------------------------- #


def test_llmclient_picks_up_app_stored_key_after_put(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A client built against the empty env has no OpenRouter key configured.
    llm = LLMClient()
    assert llm._openrouter.configured is False

    # Save a key from the app; the effective config is invalidated by the PUT.
    client.put("/api/llm/providers", json={"openrouter_api_key": "sk-or-live-334455667788"})

    # The next call re-syncs the client in place from the new effective config.
    llm._sync_config()
    assert llm._openrouter.api_key == "sk-or-live-334455667788"
    assert llm._openrouter.configured is True
    assert llm._openrouter in llm.providers


def test_get_effective_reflects_db_override(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_env(monkeypatch, llm_provider="openrouter")
    assert runtime.get_effective().primary_provider == "openrouter"
    client.put("/api/llm/providers", json={"primary_provider": "gemini"})
    # The PUT invalidated the cache; the effective primary now follows the DB.
    assert runtime.get_effective().primary_provider == "gemini"


# --------------------------------------------------------------------------- #
# /api/health reflects the effective (app-stored) provider config
# --------------------------------------------------------------------------- #


def test_health_reflects_app_stored_key_and_primary(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Env has no keys: health reports both providers unconfigured.
    before = {p["provider"]: p for p in client.get("/api/health").json()["providers"]}
    assert before["gemini"]["configured"] is False

    client.put(
        "/api/llm/providers",
        json={"gemini_api_key": "gm-health-13579", "primary_provider": "gemini"},
    )

    after = {p["provider"]: p for p in client.get("/api/health").json()["providers"]}
    assert after["gemini"]["configured"] is True
    assert after["gemini"]["is_primary"] is True
    assert after["openrouter"]["is_primary"] is False
    assert after["gemini"]["model"] == "gemini-2.0-flash"
