"""Application configuration loaded from the .env file (blueprint section 9).

Exposes a ``Settings`` model (pydantic-settings) and a cached ``get_settings()``
accessor used throughout the backend. All env keys have sensible defaults so the
app can boot without a .env file (LLM calls simply fail closed until keys are
provided).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root = trade-max/ (config.py lives at trade-max/backend/app/config.py).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Look for a .env at the project root first, then a backend-local one (the latter
# wins on conflicts). Absolute paths make this independent of the process cwd.
_ENV_FILES = (
    str(_PROJECT_ROOT / ".env"),
    str(_PROJECT_ROOT / "backend" / ".env"),
)


class Settings(BaseSettings):
    """Typed view over the .env configuration."""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILES,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- LLM providers ---
    llm_provider: str = Field(default="openrouter", alias="LLM_PROVIDER")
    openrouter_api_key: str = Field(default="", alias="OPENROUTER_API_KEY")
    openrouter_model: str = Field(default="openai/gpt-4o-mini", alias="OPENROUTER_MODEL")
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-2.0-flash", alias="GEMINI_MODEL")
    llm_fallback_enabled: bool = Field(default=True, alias="LLM_FALLBACK_ENABLED")

    # --- Persistence / server ---
    db_path: str = Field(default="./trademax.db", alias="DB_PATH")
    host: str = Field(default="127.0.0.1", alias="HOST")
    port: int = Field(default=8000, alias="PORT")
    cors_origins: str = Field(default="http://localhost:5173", alias="CORS_ORIGINS")
    tz_app: str = Field(default="Europe/Rome", alias="TZ_APP")

    @field_validator("llm_provider", mode="before")
    @classmethod
    def _normalize_provider(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @property
    def cors_origins_list(self) -> list[str]:
        """CORS_ORIGINS as a clean list (comma-separated in the .env)."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def openrouter_configured(self) -> bool:
        return bool(self.openrouter_api_key.strip())

    @property
    def gemini_configured(self) -> bool:
        return bool(self.gemini_api_key.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a process-wide cached ``Settings`` instance."""
    return Settings()
