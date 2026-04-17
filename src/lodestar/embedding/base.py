"""Protocol for any embedding backend."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingClient(Protocol):
    """Anything that turns text into fixed-dimension vectors."""

    @property
    def dim(self) -> int: ...

    def embed(self, text: str) -> list[float]: ...

    def embed_many(self, texts: Sequence[str]) -> list[list[float]]: ...
