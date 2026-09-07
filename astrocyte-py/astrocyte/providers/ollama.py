"""Ollama provider — a thin preset over :class:`OpenAIProvider`.

Ollama serves an OpenAI-compatible API, so no separate client is needed. This
exists for discoverability rather than capability: ``llm_provider: openai`` with
a ``base_url`` override already works, but nothing in the registry says so, and
a capability nobody can find is not really a capability.

Two ergonomics fixes over configuring OpenAI by hand: ``base_url`` defaults to
the local daemon, and ``api_key`` defaults to a placeholder because
``OpenAIProvider`` requires one while Ollama ignores it.
"""

from __future__ import annotations

import os
from typing import Any

from .openai import OpenAIProvider

DEFAULT_BASE_URL = "http://localhost:11434/v1"


class OllamaProvider(OpenAIProvider):
    """OpenAI-compatible client pointed at an Ollama daemon."""

    def __init__(
        self,
        *,
        model: str = "qwen3:8b",
        embedding_model: str = "nomic-embed-text",
        base_url: str | None = None,
        api_key: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            model=model,
            embedding_model=embedding_model,
            base_url=base_url or os.environ.get("OLLAMA_BASE_URL") or DEFAULT_BASE_URL,
            # Ollama ignores the key; OpenAIProvider refuses to construct without one.
            api_key=api_key or os.environ.get("OLLAMA_API_KEY") or "ollama",
            **kwargs,
        )
