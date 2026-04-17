"""Protocol for any chat LLM backend."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMClient(Protocol):
    def complete_json(self, system: str, user: str) -> str:
        """Return raw JSON string from a chat completion with JSON mode on."""
        ...

    def complete(self, system: str, user: str) -> str:
        """Free-form text completion."""
        ...
