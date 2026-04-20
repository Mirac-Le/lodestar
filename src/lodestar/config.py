"""Runtime configuration, loaded from environment / .env file."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

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
    # 路径搜索的"软阈值"：strength < weak_me_floor 的 Me 边在最短路径计算时
    # 被加重惩罚，让算法主动绕开弱直连去找更熟的中间人引荐；如果实在没有
    # 替代路径，仍会回退到这条弱边（标 path_kind=weak）。默认 4 表示
    # strength≤3（点头之交 / 弱认识 / 普通朋友）都倾向走引荐，只有熟朋友
    # 及以上算「可直接办成」的直连；可用 LODESTAR_WEAK_ME_FLOOR 调松或调严。
    weak_me_floor: int = Field(default=4, ge=1, le=5)
    # 签名网页解锁令牌（X-Owner-Unlock）。留空时从 db_path 派生，仅适合本机；
    # 多机部署请设置 LODESTAR_OWNER_UNLOCK_SECRET。
    owner_unlock_secret: str = Field(default="")

    # ----- Stage-2 reranker -----
    # 在 HybridSearch 之后插入的重排器：
    #   "none" → 不重排，保持现状（默认，零额外开销）。
    #   "llm"  → LLMJudgeReranker：再调一次 Qwen 把候选分成
    #            本人/桥梁/无关，治理 bi-encoder 的"角色断崖"。
    #   "bge"  → BgeReranker（cross-encoder bge-reranker-v2-m3，
    #            需要 `pip install -e .[rerank]` 装 torch）。
    reranker: Literal["none", "llm", "bge"] = Field(default="none")
    # Reranker 看的候选规模——比 top_k 大才有意义（用更宽召回换重排精度）。
    # 默认 30，对 100 量级网络已经覆盖；超过 ~50 LLM 提示词会偏长。
    reranker_recall_k: int = Field(default=30, ge=5, le=100)


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
