"""LLM backends (pluggable)."""

from lodestar.llm.base import LLMClient
from lodestar.llm.goal_parser import GoalParser
from lodestar.llm.openai_llm import OpenAICompatLLM

__all__ = ["GoalParser", "LLMClient", "OpenAICompatLLM", "get_llm_client"]


def get_llm_client() -> LLMClient:
    """Factory using the current settings."""
    from lodestar.config import get_settings

    s = get_settings()
    return OpenAICompatLLM(
        api_key=s.llm_api_key,
        base_url=s.llm_base_url,
        model=s.llm_model,
    )
