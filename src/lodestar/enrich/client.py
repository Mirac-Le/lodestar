"""Thin wrapper around the OpenAI-compatible Chat Completions endpoint.

We reuse `LODESTAR_LLM_*` settings (already configured for DashScope /
qwen-plus) instead of introducing a new env namespace. The client only
supports the JSON-object response format because every call inside
`enrich/` parses structured output.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from openai import OpenAI

from lodestar.config import get_settings


_log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Raised when the LLM call fails or returns invalid JSON."""


@dataclass
class LLMCallResult:
    """Result of one LLM call.

    `data` is the parsed JSON object. `raw` keeps the original string
    around for debugging and for the optional `--debug-prompt` flag of
    the enrich CLI."""

    data: dict[str, Any]
    raw: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LLMClient:
    """Single-process LLM client. Cheap to construct; no connection
    pooling beyond what the underlying httpx client gives us."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        s = get_settings()
        api_key = api_key or s.llm_api_key
        if not api_key:
            raise LLMError(
                "LODESTAR_LLM_API_KEY 未设置。请在 .env 中配置 DashScope/OpenAI 兼容 key。"
            )
        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url or s.llm_base_url,
            timeout=timeout,
        )
        self._model = model or s.llm_model

    @property
    def model(self) -> str:
        return self._model

    def chat_json(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.1,
    ) -> LLMCallResult:
        """Send a chat completion expecting a JSON object back.

        Falls back gracefully if the provider rejects `response_format`
        (some DashScope models don't support it for every variant) — we
        retry once without it, since we instruct the model in the system
        prompt to emit JSON anyway.
        """
        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            _log.debug("response_format=json_object 不被接受，降级重试：%s", exc)
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
            )

        choice = resp.choices[0].message.content or ""
        raw = choice.strip()
        if not raw:
            raise LLMError("LLM 返回空响应")

        # Some models wrap JSON in a ```json fence even when asked not to.
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:].lstrip()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMError(f"LLM 返回不是合法 JSON: {exc}\n--- raw ---\n{choice}") from exc
        if not isinstance(data, dict):
            raise LLMError(f"LLM 返回 JSON 不是 object，而是 {type(data).__name__}")

        usage = getattr(resp, "usage", None)
        return LLMCallResult(
            data=data,
            raw=raw,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )
