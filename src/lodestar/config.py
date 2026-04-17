"""Runtime configuration, loaded from environment / .env file."""

from __future__ import annotations

from pathlib import Path

from platformdirs import user_data_dir
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_db_path() -> Path:
    base = Path(user_data_dir("lodestar", appauthor=False))
    base.mkdir(parents=True, exist_ok=True)
    return base / "lodestar.db"


class Settings(BaseSettings):
    """All tunable settings. Override via environment variables or `.env`."""

    model_config = SettingsConfigDict(
        env_prefix="LODESTAR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_api_key: str = Field(default="")
    llm_base_url: str = Field(default="https://api.openai.com/v1")
    llm_model: str = Field(default="gpt-4o-mini")

    embedding_api_key: str = Field(default="")
    embedding_base_url: str = Field(default="https://api.openai.com/v1")
    embedding_model: str = Field(default="text-embedding-3-small")
    embedding_dim: int = Field(default=1536)
    # Providers cap how many inputs per /embeddings call differs:
    #   DashScope v3/v4 = 10, DashScope v2 = 25, OpenAI = 2048.
    # Default 10 is safe everywhere.
    embedding_batch_size: int = Field(default=10, ge=1, le=2048)

    db_path: Path = Field(default_factory=_default_db_path)

    max_hops: int = Field(default=3, ge=1, le=5)
    top_k: int = Field(default=10, ge=1, le=100)


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Force reload of settings on next access (useful for tests)."""
    global _settings
    _settings = None
