"""Offline tests for LLM model selection (per provider AND per actor) plus the
token-optimization helpers.

All upstream provider fetches and ``Settings`` are monkeypatched, so these tests
never touch the network or the real ``.env`` / ``trademax.db``. The config
endpoints use the conftest ``client`` fixture (whose ``get_db`` is overridden to a
per-test temp SQLite DB); ``app.llm.prefs`` is redirected to the same temp DB via
its ``session_scope`` for the resolution-order test.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

import app.api.llm as llm_api
import app.llm.prefs as prefs_mod
from app.models import LlmModelPref
from app.schemas import LlmModelInfo

AGENT_NAMES = (
    "technical",
    "fundamentals",
    "macro_news",
    "corporate_news",
    "sentiment",
    "synthesizer",
    "validator",
)


def _fake_settings(**overrides: object) -> SimpleNamespace:
    """An EffectiveLlmConfig-shaped stand-in exposing only the attrs the endpoints read.

    ``llm_api`` now reads the effective config (env + DB overrides) via
    ``get_effective``; these tests monkeypatch that with this stand-in so they stay
    fully offline. ``primary_provider`` mirrors the effective primary (DB override,
    else env ``LLM_PROVIDER``).
    """
    values: dict[str, object] = {
        "primary_provider": "openrouter",
        "openrouter_configured": True,
        "gemini_configured": True,
        "ollama_configured": False,
        "openrouter_model": "openai/gpt-4o-mini",
        "gemini_model": "gemini-2.0-flash",
        "ollama_model": "llama3.2:3b",
        "openrouter_api_key": "or-key",
        "gemini_api_key": "gm-key",
        "ollama_base_url": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture(autouse=True)
def _reset_caches() -> None:
    """Keep the module-level models/prefs caches from leaking across tests."""
    llm_api._MODELS_CACHE.clear()
    prefs_mod.invalidate_prefs_cache()
    yield
    llm_api._MODELS_CACHE.clear()
    prefs_mod.invalidate_prefs_cache()


# --------------------------------------------------------------------------- #
# GET /api/llm/config
# --------------------------------------------------------------------------- #


def test_get_config_defaults(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_api, "get_effective", lambda: _fake_settings(primary_provider="openrouter"))

    resp = client.get("/api/llm/config")
    assert resp.status_code == 200
    body = resp.json()

    assert body["default"] is None
    assert body["per_agent"] == {name: None for name in AGENT_NAMES}

    providers = {p["provider"]: p for p in body["providers"]}
    assert set(providers) == {"openrouter", "gemini", "ollama"}
    assert providers["openrouter"]["configured"] is True
    assert providers["openrouter"]["is_primary"] is True
    assert providers["gemini"]["is_primary"] is False
    assert providers["ollama"]["is_primary"] is False
    assert providers["openrouter"]["env_default_model"] == "openai/gpt-4o-mini"
    assert providers["gemini"]["env_default_model"] == "gemini-2.0-flash"
    assert providers["ollama"]["env_default_model"] == "llama3.2:3b"


def test_get_config_primary_follows_env_provider(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm_api, "get_effective", lambda: _fake_settings(primary_provider="gemini"))
    providers = {p["provider"]: p for p in client.get("/api/llm/config").json()["providers"]}
    assert providers["gemini"]["is_primary"] is True
    assert providers["openrouter"]["is_primary"] is False


# --------------------------------------------------------------------------- #
# PUT /api/llm/config
# --------------------------------------------------------------------------- #


def test_put_config_roundtrip(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_api, "get_effective", lambda: _fake_settings())

    resp = client.put(
        "/api/llm/config",
        json={
            "default": {"provider": "openrouter", "model": "openai/gpt-4o"},
            "per_agent": {"technical": {"provider": "gemini", "model": "gemini-2.0-flash"}},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["default"] == {"provider": "openrouter", "model": "openai/gpt-4o"}
    assert body["per_agent"]["technical"] == {"provider": "gemini", "model": "gemini-2.0-flash"}
    assert body["per_agent"]["fundamentals"] is None
    # The effective primary provider now follows the DB default row.
    providers = {p["provider"]: p for p in body["providers"]}
    assert providers["openrouter"]["is_primary"] is True

    # A fresh GET reflects the persisted state.
    reread = client.get("/api/llm/config").json()
    assert reread["default"]["model"] == "openai/gpt-4o"
    assert reread["per_agent"]["technical"]["provider"] == "gemini"

    # Reset with null unsets the rows.
    resp2 = client.put(
        "/api/llm/config",
        json={"default": None, "per_agent": {"technical": None}},
    )
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["default"] is None
    assert body2["per_agent"]["technical"] is None
    # is_primary falls back to the env provider once the default row is gone.
    providers2 = {p["provider"]: p for p in body2["providers"]}
    assert providers2["openrouter"]["is_primary"] is True


def test_put_config_model_is_trimmed(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_api, "get_effective", lambda: _fake_settings())
    resp = client.put(
        "/api/llm/config",
        json={"default": {"provider": "openrouter", "model": "  openai/gpt-4o  "}, "per_agent": {}},
    )
    assert resp.status_code == 200
    assert resp.json()["default"]["model"] == "openai/gpt-4o"


def test_put_config_unknown_provider_returns_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm_api, "get_effective", lambda: _fake_settings())
    resp = client.put(
        "/api/llm/config",
        json={"default": {"provider": "cohere", "model": "command"}, "per_agent": {}},
    )
    assert resp.status_code == 400
    assert "Provider non valido" in resp.json()["detail"]


def test_put_config_unconfigured_provider_returns_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm_api, "get_effective", lambda: _fake_settings(gemini_configured=False))
    resp = client.put(
        "/api/llm/config",
        json={"default": {"provider": "gemini", "model": "gemini-2.0-flash"}, "per_agent": {}},
    )
    assert resp.status_code == 400
    assert "non configurato" in resp.json()["detail"]


def test_put_config_empty_model_returns_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm_api, "get_effective", lambda: _fake_settings())
    resp = client.put(
        "/api/llm/config",
        json={"default": {"provider": "openrouter", "model": "   "}, "per_agent": {}},
    )
    assert resp.status_code == 400
    assert "stringa non vuota" in resp.json()["detail"]


def test_put_config_rejects_invalid_before_writing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single invalid slot fails the whole PUT without persisting any row."""
    monkeypatch.setattr(llm_api, "get_effective", lambda: _fake_settings())
    resp = client.put(
        "/api/llm/config",
        json={
            "default": {"provider": "openrouter", "model": "openai/gpt-4o"},
            "per_agent": {"technical": {"provider": "cohere", "model": "x"}},
        },
    )
    assert resp.status_code == 400
    # Nothing was written: the valid default slot must not have leaked through.
    assert client.get("/api/llm/config").json()["default"] is None


# --------------------------------------------------------------------------- #
# GET /api/llm/models
# --------------------------------------------------------------------------- #


def test_get_models_openrouter(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_api, "get_effective", lambda: _fake_settings())

    async def fake_fetch(api_key: str) -> list[LlmModelInfo]:
        assert api_key == "or-key"
        return [
            LlmModelInfo(id="anthropic/claude", label="Claude"),
            LlmModelInfo(id="openai/gpt-4o", label="GPT-4o"),
        ]

    monkeypatch.setattr(llm_api, "_fetch_openrouter_models", fake_fetch)

    resp = client.get("/api/llm/models", params={"provider": "openrouter"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "openrouter"
    assert [m["id"] for m in body["models"]] == ["anthropic/claude", "openai/gpt-4o"]
    assert body["models"][1]["label"] == "GPT-4o"
    assert "fetched_at" in body


def test_get_models_gemini(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_api, "get_effective", lambda: _fake_settings())

    async def fake_fetch(api_key: str) -> list[LlmModelInfo]:
        assert api_key == "gm-key"
        return [LlmModelInfo(id="gemini-2.0-flash", label="Gemini 2.0 Flash")]

    monkeypatch.setattr(llm_api, "_fetch_gemini_models", fake_fetch)

    resp = client.get("/api/llm/models", params={"provider": "gemini"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "gemini"
    assert body["models"] == [{"id": "gemini-2.0-flash", "label": "Gemini 2.0 Flash"}]


def test_get_models_caches_within_ttl(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm_api, "get_effective", lambda: _fake_settings())
    calls = {"n": 0}

    async def fake_fetch(api_key: str) -> list[LlmModelInfo]:
        calls["n"] += 1
        return [LlmModelInfo(id="openai/gpt-4o", label="GPT-4o")]

    monkeypatch.setattr(llm_api, "_fetch_openrouter_models", fake_fetch)

    first = client.get("/api/llm/models", params={"provider": "openrouter"})
    second = client.get("/api/llm/models", params={"provider": "openrouter"})
    assert first.status_code == second.status_code == 200
    assert calls["n"] == 1  # second call served from the 1h cache


def test_get_models_unknown_provider_returns_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm_api, "get_effective", lambda: _fake_settings())
    resp = client.get("/api/llm/models", params={"provider": "cohere"})
    assert resp.status_code == 400
    assert "Provider non valido" in resp.json()["detail"]


def test_get_models_unconfigured_returns_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        llm_api, "get_effective", lambda: _fake_settings(openrouter_configured=False)
    )
    resp = client.get("/api/llm/models", params={"provider": "openrouter"})
    assert resp.status_code == 400
    assert "non configurato" in resp.json()["detail"]


def test_get_models_upstream_failure_returns_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm_api, "get_effective", lambda: _fake_settings())

    async def boom(api_key: str) -> list[LlmModelInfo]:
        raise RuntimeError("upstream down")

    monkeypatch.setattr(llm_api, "_fetch_openrouter_models", boom)

    resp = client.get("/api/llm/models", params={"provider": "openrouter"})
    assert resp.status_code == 502
    assert "Impossibile" in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# Prefs resolution order (agent beats default beats none)
# --------------------------------------------------------------------------- #


def test_prefs_resolution_order(
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
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

    monkeypatch.setattr(prefs_mod, "session_scope", _scope)
    prefs_mod.invalidate_prefs_cache()

    # No rows -> no preference.
    assert prefs_mod.get_pref("technical") is None

    # A "default" row is used when there is no agent-specific row.
    with _scope() as db:
        db.add(LlmModelPref(agent_name="default", provider="openrouter", model="default-model"))
    prefs_mod.invalidate_prefs_cache()
    assert prefs_mod.get_pref("technical") == ("openrouter", "default-model")

    # An agent-specific row wins over the default; other agents still see default.
    with _scope() as db:
        db.add(LlmModelPref(agent_name="technical", provider="gemini", model="tech-model"))
    prefs_mod.invalidate_prefs_cache()
    assert prefs_mod.get_pref("technical") == ("gemini", "tech-model")
    assert prefs_mod.get_pref("fundamentals") == ("openrouter", "default-model")


def test_prefs_db_error_resolves_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def _boom():
        raise RuntimeError("db down")
        yield  # pragma: no cover

    monkeypatch.setattr(prefs_mod, "session_scope", _boom)
    prefs_mod.invalidate_prefs_cache()
    assert prefs_mod.get_pref("technical") is None


# --------------------------------------------------------------------------- #
# Token-optimization helpers
# --------------------------------------------------------------------------- #


def test_compact_json_rounds_floats() -> None:
    import json

    from app.agents.base import compact_json

    payload = {
        "big": 333.739990234375,
        "small": 0.123456789,
        "series": [12345.6789, 1.0],
        "count": 42,
        "flag": True,
    }
    result = json.loads(compact_json(payload))
    assert result["big"] == 333.74  # abs >= 100 -> 2 decimals
    assert result["small"] == 0.1235  # abs < 100 -> 4 decimals
    assert result["series"] == [12345.68, 1.0]
    assert result["count"] == 42  # ints untouched
    assert result["flag"] is True  # bools untouched


def test_deterministic_skips_when_inputs_empty() -> None:
    from app.engine.orchestrator import _deterministic_analyst_outputs

    data = {"macro_news": [], "corporate_news": [], "fundamentals": {}, "sentiment": {}}
    skips = _deterministic_analyst_outputs(data)

    assert set(skips) == {"macro_news", "corporate_news", "fundamentals", "sentiment"}
    assert "technical" not in skips  # the technical analyst is never skipped
    for name, out in skips.items():
        assert out["stance"] == "NEUTRAL"
        assert out["signal"] == 0.0
        assert out["confidence"] == 0.2
        assert out["data_quality"] == "POOR"
        assert out["key_points"] == []
        assert out["risks"] == []
        assert out["summary_it"]  # non-empty Italian text for the UI
    assert "societaria" in skips["corporate_news"]["summary_it"]
    assert "fondamentali" in skips["fundamentals"]["summary_it"].lower()


def test_deterministic_no_skips_when_data_present() -> None:
    from app.engine.orchestrator import _deterministic_analyst_outputs

    data = {
        "macro_news": [{"title": "Fed holds rates"}],
        "corporate_news": [{"title": "Q3 earnings beat"}],
        "fundamentals": {
            "pe": 20.0,
            "forward_pe": None,
            "eps": None,
            "market_cap": None,
            "beta": None,
        },
        "sentiment": {"recommendation_mean": 2.0},
    }
    assert _deterministic_analyst_outputs(data) == {}


def test_deterministic_skip_output_is_ok_shaped() -> None:
    """A deterministic skip becomes an OK AgentResult (never counted as FAILED)."""
    from app.agents.base import AgentResult
    from app.engine.orchestrator import _DETERMINISTIC_PROVIDER, _deterministic_analyst_outputs

    skips = _deterministic_analyst_outputs({"macro_news": [], "corporate_news": [{"x": 1}]})
    # Only macro_news is skipped here (corporate has an item, fundamentals absent -> empty).
    assert "macro_news" in skips
    result = AgentResult(
        agent_name="macro_news", output=skips["macro_news"], provider=_DETERMINISTIC_PROVIDER
    )
    assert not isinstance(result, BaseException)  # -> persisted status OK, no LLM call made
    assert result.provider == "deterministic"


def test_slim_analyst_outputs_drops_summary_it() -> None:
    from app.engine.orchestrator import _slim_analyst_outputs

    outputs = {
        "technical": {
            "stance": "BULLISH",
            "signal": 0.5,
            "confidence": 0.7,
            "trend": "UP",
            "summary_it": "testo italiano lungo che i decisori non leggono",
        },
        "fundamentals": None,
    }
    slim = _slim_analyst_outputs(outputs)
    assert "summary_it" not in slim["technical"]
    assert slim["technical"]["trend"] == "UP"  # agent-specific extra field preserved
    assert slim["technical"]["stance"] == "BULLISH"
    assert slim["fundamentals"] is None
    # The original is untouched (persisted rows keep the full output).
    assert outputs["technical"]["summary_it"]


def test_slim_news_caps_items_and_truncates_summary() -> None:
    from app.engine.orchestrator import (
        _MAX_NEWS_ITEMS,
        _MAX_NEWS_SUMMARY_CHARS,
        _slim_news,
    )

    items = [{"title": f"n{i}", "summary": "x" * 500} for i in range(15)]
    slim = _slim_news(items)
    assert len(slim) == _MAX_NEWS_ITEMS
    assert all(len(entry["summary"]) == _MAX_NEWS_SUMMARY_CHARS for entry in slim)
    assert _slim_news([]) == []
    assert _slim_news(None) == []
