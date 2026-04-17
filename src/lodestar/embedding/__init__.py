"""Embedding backends (pluggable)."""

from lodestar.embedding.base import EmbeddingClient
from lodestar.embedding.openai_emb import OpenAICompatEmbedding

__all__ = ["EmbeddingClient", "OpenAICompatEmbedding", "get_embedding_client"]


def get_embedding_client() -> EmbeddingClient:
    """Factory using the current settings."""
    from lodestar.config import get_settings

    s = get_settings()
    return OpenAICompatEmbedding(
        api_key=s.embedding_api_key,
        base_url=s.embedding_base_url,
        model=s.embedding_model,
        dim=s.embedding_dim,
        batch_size=s.embedding_batch_size,
    )
