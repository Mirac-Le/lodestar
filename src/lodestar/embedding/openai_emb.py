"""Embedding via any OpenAI-compatible /v1/embeddings endpoint."""

from __future__ import annotations

from collections.abc import Sequence

from openai import OpenAI


class OpenAICompatEmbedding:
    """Works with OpenAI, DashScope (Aliyun Bailian), DeepSeek, Zhipu, Kimi, etc.

    Automatically batches large input lists to stay within provider limits.
    DashScope caps v3/v4 at 10 inputs per call; OpenAI allows up to 2048.
    The default `batch_size` of 10 is safe everywhere.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        dim: int,
        batch_size: int = 10,
    ) -> None:
        if not api_key:
            raise RuntimeError(
                "Embedding API key not set. Put LODESTAR_EMBEDDING_API_KEY in your .env "
                "or export it as an environment variable."
            )
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model = model
        self._dim = dim
        self._batch_size = max(1, batch_size)

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            chunk = list(texts[start : start + self._batch_size])
            resp = self._client.embeddings.create(model=self._model, input=chunk)
            for item in resp.data:
                if len(item.embedding) != self._dim:
                    raise RuntimeError(
                        f"Embedding dim mismatch: model returned {len(item.embedding)}, "
                        f"but LODESTAR_EMBEDDING_DIM is {self._dim}. "
                        "Set LODESTAR_EMBEDDING_DIM to the correct value for your model."
                    )
                out.append(list(item.embedding))
        return out
