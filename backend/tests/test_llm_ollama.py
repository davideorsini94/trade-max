"""Offline tests for the local Ollama provider (third LLM provider).

Covers the :class:`OllamaProvider` chat adapter, the DB-backed ``ollama_base_url``
provider setting (``GET``/``PUT`` ``/api/llm/providers`` roundtrip + validation),
the Ollama branches of ``GET /api/llm/models`` and ``POST /api/llm/providers/test``,
and the new ``/api/llm/ollama/library`` + in-app pull endpoints.

Everything is fully offline: ``app.llm.runtime`` is redirected to the per-test
temp SQLite DB and a fake env ``Settings`` (mirroring ``test_llm_providers.py``);
every upstream Ollama HTTP call is monkeypatched. No network, no real ``.env`` /
``trademax.db``.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

import app.api.llm as llm_api
import app.llm.runtime as runtime
from app.llm.providers import OllamaProvider, ProviderError


def _env(**overrides: object) -> SimpleNamespace:
    """A Settings-shaped stand-in for the env layer that ``runtime`` reads."""
    values: dict[str, object] = {
        "openrouter_api_key": "",
        "gemini_api_key": "",
        "ollama_base_url": "",
        "llm_provider": "openrouter",
        "llm_fallback_enabled": True,
        "openrouter_model": "openai/gpt-4o-mini",
        "gemini_model": "gemini-2.0-flash",
        "ollama_model": "llama3.2:3b",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture(autouse=True)
def _provider_runtime(
    monkeypatch: pytest.MonkeyPatch, db_session_factory: sessionmaker[Session]
) -> None:
    """Point ``runtime`` at the per-test temp DB + a no-keys env, and reset caches.

    Shares the SAME ``db_session_factory`` as the ``client`` fixture, so
    ``runtime.get_effective`` reads exactly the rows the endpoints write. Also
    clears the module-level Ollama pull state between tests.
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
    llm_api._PULL_STATES.clear()
    llm_api._PULL_TASKS.clear()
    yield
    runtime.invalidate_effective()
    llm_api._MODELS_CACHE.clear()
    llm_api._PULL_STATES.clear()
    llm_api._PULL_TASKS.clear()


def _set_env(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    monkeypatch.setattr(runtime, "get_settings", lambda: _env(**overrides))
    runtime.invalidate_effective()


# --------------------------------------------------------------------------- #
# OllamaProvider (chat adapter)
# --------------------------------------------------------------------------- #


async def test_ollama_provider_payload_shape_and_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    async def fake_post(self, url: str, *, headers=None, json: dict) -> dict:  # noqa: ANN001
        captured["url"] = url
        captured["payload"] = json
        return {
            "message": {"role": "assistant", "content": '{"stance": "NEUTRAL"}'},
            "prompt_eval_count": 123,
            "eval_count": 45,
        }

    monkeypatch.setattr(OllamaProvider, "_post", fake_post)
    provider = OllamaProvider("http://localhost:11434/", "llama3.2:3b")

    content = await provider.chat("SYSTEM", "USER", 0.3, 512)

    assert content == '{"stance": "NEUTRAL"}'
    # Trailing slash on the base URL is normalised away.
    assert captured["url"] == "http://localhost:11434/api/chat"
    payload = captured["payload"]
    assert payload["model"] == "llama3.2:3b"
    assert payload["stream"] is False
    assert payload["format"] == "json"
    assert payload["messages"] == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "USER"},
    ]
    assert payload["options"] == {"temperature": 0.3, "num_predict": 512}
    # Usage is parsed from prompt_eval_count / eval_count.
    assert provider.last_usage == (123, 45)


async def test_ollama_provider_model_override_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    async def fake_post(self, url: str, *, headers=None, json: dict) -> dict:  # noqa: ANN001
        captured["payload"] = json
        return {"message": {"content": "{}"}}

    monkeypatch.setattr(OllamaProvider, "_post", fake_post)
    provider = OllamaProvider("http://localhost:11434", "llama3.2:3b")

    await provider.chat("s", "u", 0.2, 100, model="qwen2.5:7b")
    assert captured["payload"]["model"] == "qwen2.5:7b"


async def test_ollama_provider_unreachable_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OllamaProvider("http://localhost:11434", "llama3.2:3b")

    async def boom(*args: object, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    # Fail at the transport layer so the provider's own error mapping runs.
    monkeypatch.setattr(provider._client, "post", boom)

    with pytest.raises(ProviderError) as excinfo:
        await provider.chat("s", "u", 0.2, 100)
    assert excinfo.value.retryable is True
    assert "non raggiungibile" in str(excinfo.value)
    assert "http://localhost:11434" in str(excinfo.value)


def test_ollama_provider_configured_tracks_base_url() -> None:
    assert OllamaProvider("", "m").configured is False
    assert OllamaProvider("   ", "m").configured is False
    assert OllamaProvider("http://localhost:11434", "m").configured is True


# --------------------------------------------------------------------------- #
# GET / PUT /api/llm/providers (ollama_base_url)
# --------------------------------------------------------------------------- #


def test_get_providers_includes_ollama_entry(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = client.get("/api/llm/providers").json()
    providers = {p["provider"]: p for p in body["providers"]}
    assert set(providers) == {"openrouter", "gemini", "ollama"}
    # Cloud entries gain an explicit null base_url.
    assert providers["openrouter"]["base_url"] is None
    assert providers["gemini"]["base_url"] is None
    # Ollama is keyless and, with an empty env URL, unconfigured.
    ollama = providers["ollama"]
    assert ollama["configured"] is False
    assert ollama["source"] is None
    assert ollama["key_masked"] is None
    assert ollama["base_url"] is None
    assert ollama["default_model"] == "llama3.2:3b"


def test_put_ollama_base_url_roundtrip_source_app(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    resp = client.put(
        "/api/llm/providers", json={"ollama_base_url": "  http://localhost:11434  "}
    )
    assert resp.status_code == 200
    ollama = {p["provider"]: p for p in resp.json()["providers"]}["ollama"]
    assert ollama["configured"] is True
    assert ollama["source"] == "app"
    assert ollama["base_url"] == "http://localhost:11434"  # trimmed
    assert ollama["key_masked"] is None

    # A fresh GET reflects the persisted state.
    reread = {p["provider"]: p for p in client.get("/api/llm/providers").json()["providers"]}
    assert reread["ollama"]["base_url"] == "http://localhost:11434"


def test_put_ollama_base_url_env_fallback_and_null_reset(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_env(monkeypatch, ollama_base_url="http://env-host:11434")

    # Store an app URL: it wins over env (source "app").
    stored = client.put(
        "/api/llm/providers", json={"ollama_base_url": "http://app-host:11434"}
    ).json()
    assert {p["provider"]: p for p in stored["providers"]}["ollama"]["source"] == "app"

    # null deletes the stored URL; the env URL remains as fallback (source "env").
    reset = client.put("/api/llm/providers", json={"ollama_base_url": None}).json()
    ollama = {p["provider"]: p for p in reset["providers"]}["ollama"]
    assert ollama["configured"] is True
    assert ollama["source"] == "env"
    assert ollama["base_url"] == "http://env-host:11434"


def test_put_ollama_base_url_invalid_returns_400(client: TestClient) -> None:
    resp = client.put("/api/llm/providers", json={"ollama_base_url": "localhost:11434"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "URL di Ollama non valido."
    # Nothing was persisted: Ollama stays unconfigured.
    ollama = {p["provider"]: p for p in client.get("/api/llm/providers").json()["providers"]}[
        "ollama"
    ]
    assert ollama["configured"] is False


def test_put_primary_provider_ollama(client: TestClient) -> None:
    resp = client.put("/api/llm/providers", json={"primary_provider": "ollama"})
    assert resp.status_code == 200
    assert resp.json()["primary_provider"] == "ollama"


# --------------------------------------------------------------------------- #
# GET /api/llm/models?provider=ollama
# --------------------------------------------------------------------------- #


def _fake_tags() -> list[dict]:
    return [
        {"name": "qwen2.5:7b", "size": 4_700_000_000, "details": {"parameter_size": "7.6B"}},
        {"name": "llama3.2:3b", "size": 2_000_000_000, "details": {"parameter_size": "3.2B"}},
    ]


def test_get_models_ollama(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    client.put("/api/llm/providers", json={"ollama_base_url": "http://localhost:11434"})

    async def fake_fetch(base_url: str) -> list[dict]:
        assert base_url == "http://localhost:11434"
        return _fake_tags()

    monkeypatch.setattr(llm_api, "_fetch_ollama_tags", fake_fetch)

    resp = client.get("/api/llm/models", params={"provider": "ollama"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "ollama"
    # id == model name, label includes the parameter size, sorted by id.
    assert [m["id"] for m in body["models"]] == ["llama3.2:3b", "qwen2.5:7b"]
    assert body["models"][0]["label"] == "llama3.2:3b (3.2B)"


def test_get_models_ollama_not_configured_returns_400(client: TestClient) -> None:
    resp = client.get("/api/llm/models", params={"provider": "ollama"})
    assert resp.status_code == 400
    assert "non configurato" in resp.json()["detail"]


def test_get_models_ollama_unreachable_returns_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.put("/api/llm/providers", json={"ollama_base_url": "http://localhost:11434"})

    async def boom(base_url: str) -> list[dict]:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(llm_api, "_fetch_ollama_tags", boom)
    resp = client.get("/api/llm/models", params={"provider": "ollama"})
    assert resp.status_code == 502


# --------------------------------------------------------------------------- #
# GET /api/llm/ollama/library
# --------------------------------------------------------------------------- #


def test_ollama_library_installed_flags(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.put("/api/llm/providers", json={"ollama_base_url": "http://localhost:11434"})

    async def fake_fetch(base_url: str) -> list[dict]:
        return [
            # exact catalog id match
            {"name": "qwen2.5:7b", "size": 4_700_000_000, "details": {"parameter_size": "7.6B"}},
            # prefix match (family "llama3.2") for catalog "llama3.2:3b"
            {"name": "llama3.2:latest", "size": 2_000_000_000, "details": {}},
        ]

    monkeypatch.setattr(llm_api, "_fetch_ollama_tags", fake_fetch)

    body = client.get("/api/llm/ollama/library").json()

    installed_ids = {m["id"] for m in body["installed"]}
    assert installed_ids == {"qwen2.5:7b", "llama3.2:latest"}
    # size_bytes is carried through.
    by_id = {m["id"]: m for m in body["installed"]}
    assert by_id["qwen2.5:7b"]["size_bytes"] == 4_700_000_000

    catalog = {c["id"]: c for c in body["catalog"]}
    assert catalog["qwen2.5:7b"]["installed"] is True  # exact match
    assert catalog["llama3.2:3b"]["installed"] is True  # family-prefix match
    assert catalog["mistral:7b"]["installed"] is False
    # Curated catalog carries Italian descriptions + size hints.
    assert catalog["mistral:7b"]["description_it"]
    assert catalog["mistral:7b"]["size_hint"]


def test_ollama_library_not_configured_returns_400(client: TestClient) -> None:
    resp = client.get("/api/llm/ollama/library")
    assert resp.status_code == 400


def test_ollama_library_unreachable_returns_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.put("/api/llm/providers", json={"ollama_base_url": "http://localhost:11434"})

    async def boom(base_url: str) -> list[dict]:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(llm_api, "_fetch_ollama_tags", boom)
    resp = client.get("/api/llm/ollama/library")
    assert resp.status_code == 502
    assert "non raggiungibile" in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# POST /api/llm/providers/test (ollama)
# --------------------------------------------------------------------------- #


def test_test_endpoint_ollama_ok(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    client.put("/api/llm/providers", json={"ollama_base_url": "http://localhost:11434"})

    async def fake_reachable(base_url: str) -> None:
        assert base_url == "http://localhost:11434"

    monkeypatch.setattr(llm_api, "_check_ollama_reachable", fake_reachable)
    resp = client.post("/api/llm/providers/test", json={"provider": "ollama"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_test_endpoint_ollama_unreachable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.put("/api/llm/providers", json={"ollama_base_url": "http://localhost:11434"})

    async def boom(base_url: str) -> None:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(llm_api, "_check_ollama_reachable", boom)
    resp = client.post("/api/llm/providers/test", json={"provider": "ollama"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "non raggiungibile" in body["detail_it"]


def test_test_endpoint_ollama_not_configured_returns_400(client: TestClient) -> None:
    resp = client.post("/api/llm/providers/test", json={"provider": "ollama"})
    assert resp.status_code == 400
    assert "non configurato" in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# In-app pull: POST /api/llm/ollama/pull + GET /api/llm/ollama/pull/status
# --------------------------------------------------------------------------- #


def _configured_ollama() -> SimpleNamespace:
    return SimpleNamespace(
        ollama_configured=True, ollama_base_url="http://localhost:11434"
    )


async def test_ollama_pull_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_api, "get_effective", _configured_ollama)

    async def fake_reachable(base_url: str) -> None:
        return None

    async def fake_stream(base_url: str, model: str):
        yield {"status": "pulling manifest"}
        yield {"status": "downloading", "total": 1000, "completed": 500}
        yield {"status": "downloading", "total": 1000, "completed": 1000}
        yield {"status": "success"}

    monkeypatch.setattr(llm_api, "_check_ollama_reachable", fake_reachable)
    monkeypatch.setattr(llm_api, "_stream_ollama_pull", fake_stream)

    started = await llm_api.ollama_pull(llm_api.OllamaPullRequest(model="llama3.2:3b"))
    assert started == {"detail": "Download avviato."}

    # Drive the background task to completion, then read the final status.
    await llm_api._PULL_TASKS["llama3.2:3b"]
    status = llm_api.ollama_pull_status(model="llama3.2:3b")
    assert status.status == "success"
    assert status.total_bytes == 1000
    assert status.completed_bytes == 1000
    assert status.percent == 100.0


async def test_ollama_pull_double_start_returns_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm_api, "get_effective", _configured_ollama)

    async def fake_reachable(base_url: str) -> None:
        return None

    # A stream that never completes keeps the first pull in the "pulling" state.
    async def blocking_stream(base_url: str, model: str):
        yield {"status": "pulling manifest"}
        await asyncio.Event().wait()  # pragma: no cover - blocks until cancelled
        yield {"status": "success"}  # pragma: no cover

    monkeypatch.setattr(llm_api, "_check_ollama_reachable", fake_reachable)
    monkeypatch.setattr(llm_api, "_stream_ollama_pull", blocking_stream)

    await llm_api.ollama_pull(llm_api.OllamaPullRequest(model="llama3.2:3b"))

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await llm_api.ollama_pull(llm_api.OllamaPullRequest(model="llama3.2:3b"))
    assert excinfo.value.status_code == 409

    # Clean up the still-running background task within this test's event loop.
    task = llm_api._PULL_TASKS["llama3.2:3b"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_ollama_pull_empty_model_returns_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm_api, "get_effective", _configured_ollama)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await llm_api.ollama_pull(llm_api.OllamaPullRequest(model="   "))
    assert excinfo.value.status_code == 400


async def test_ollama_pull_not_configured_returns_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        llm_api,
        "get_effective",
        lambda: SimpleNamespace(ollama_configured=False, ollama_base_url=""),
    )
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await llm_api.ollama_pull(llm_api.OllamaPullRequest(model="llama3.2:3b"))
    assert excinfo.value.status_code == 400


async def test_ollama_pull_unreachable_returns_502(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm_api, "get_effective", _configured_ollama)

    async def boom(base_url: str) -> None:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(llm_api, "_check_ollama_reachable", boom)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await llm_api.ollama_pull(llm_api.OllamaPullRequest(model="llama3.2:3b"))
    assert excinfo.value.status_code == 502


def test_ollama_pull_status_idle_when_never_started() -> None:
    status = llm_api.ollama_pull_status(model="never-pulled")
    assert status.status == "idle"
    assert status.model == "never-pulled"
    assert status.percent is None


async def test_ollama_pull_stream_error_sets_error_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm_api, "get_effective", _configured_ollama)

    async def fake_reachable(base_url: str) -> None:
        return None

    async def failing_stream(base_url: str, model: str):
        yield {"status": "pulling manifest"}
        raise httpx.ReadError("stream dropped")

    monkeypatch.setattr(llm_api, "_check_ollama_reachable", fake_reachable)
    monkeypatch.setattr(llm_api, "_stream_ollama_pull", failing_stream)

    await llm_api.ollama_pull(llm_api.OllamaPullRequest(model="llama3.2:3b"))
    await llm_api._PULL_TASKS["llama3.2:3b"]
    status = llm_api.ollama_pull_status(model="llama3.2:3b")
    assert status.status == "error"
    assert "Errore" in status.detail_it
