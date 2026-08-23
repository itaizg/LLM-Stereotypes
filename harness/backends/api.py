"""Thin OpenAI-compatible chat-completions adapter.

Points at any OpenAI-compatible endpoint: Anthropic/OpenAI/Gemini directly,
or a locally-hosted vLLM / llama.cpp server via `base_url`. This is what
keeps vLLM an optional endpoint rather than a harness dependency — run it
yourself as a local server and point this backend at it.

Requires `openai` (a thin HTTP client, not a heavyweight framework) and
network access; not used by tests.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from harness.backends.base import BackendDescriptor, ModelBackend


class ApiBackend(ModelBackend):
    def __init__(
        self,
        model_id: str,
        *,
        base_url: str | None = None,
        api_key_env: str = "ANTHROPIC_API_KEY",
        max_tokens: int = 50,
    ) -> None:
        from openai import OpenAI

        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise RuntimeError(f"missing API key: set {api_key_env}")

        self._model_id = model_id
        self._base_url = base_url
        self._max_tokens = max_tokens
        self._client = OpenAI(api_key=api_key, base_url=base_url)

    @property
    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            name="api", model_id=self._model_id, device=self._base_url or "api"
        )

    def generate(
        self,
        prompt: str,
        *,
        seed: int,
        temperature: float,
        context: Mapping[str, str] | None = None,
    ) -> str:
        response = self._client.chat.completions.create(
            model=self._model_id,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=self._max_tokens,
            seed=seed,
        )
        return response.choices[0].message.content or ""
