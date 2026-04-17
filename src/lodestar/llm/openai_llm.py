"""Chat LLM via any OpenAI-compatible /v1/chat/completions endpoint."""

from __future__ import annotations

from openai import OpenAI


class OpenAICompatLLM:
    """Works with OpenAI, DeepSeek, Zhipu, Kimi, etc."""

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        if not api_key:
            raise RuntimeError(
                "LLM API key not set. Put LODESTAR_LLM_API_KEY in your .env "
                "or export it as an environment variable."
            )
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model = model

    def complete_json(self, system: str, user: str) -> str:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        content = resp.choices[0].message.content
        return content or "{}"

    def complete(self, system: str, user: str) -> str:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.3,
        )
        return resp.choices[0].message.content or ""
