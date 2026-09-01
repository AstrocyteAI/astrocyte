"""Composite LLMProvider — split completion and embedding across two backends.

Delegates ``complete()`` to one provider and ``embed()`` to another. The
motivating composition is Claude-CLI completions (subscription auth, no
embeddings surface) + local sentence-transformers embeddings::

    from astrocyte.providers.claude_cli import ClaudeCliProvider
    from astrocyte.providers.composite import CompositeLLMProvider
    from astrocyte.providers.local_embeddings import LocalEmbeddingsProvider

    provider = CompositeLLMProvider(
        completion_provider=ClaudeCliProvider(model="haiku"),
        embedding_provider=LocalEmbeddingsProvider(),
    )

Any pair of LLMProvider implementations composes the same way.
"""

from __future__ import annotations

from typing import Any, ClassVar

from astrocyte.types import Completion, LLMCapabilities, Message, ToolDefinition


class CompositeLLMProvider:
    """LLMProvider that routes complete() and embed() to different backends."""

    SPI_VERSION: ClassVar[int] = 1

    def __init__(
        self,
        *,
        completion_provider: Any,
        embedding_provider: Any,
    ) -> None:
        self._completion = completion_provider
        self._embedding = embedding_provider

    def capabilities(self) -> LLMCapabilities:
        comp = self._completion.capabilities()
        emb = self._embedding.capabilities()
        return LLMCapabilities(
            supports_multimodal_completion=comp.supports_multimodal_completion,
            modalities_supported=comp.modalities_supported,
            supports_multimodal_embedding=emb.supports_multimodal_embedding,
            supports_batch_embed=emb.supports_batch_embed,
        )

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = None,
        response_format: dict | None = None,
    ) -> Completion:
        return await self._completion.complete(
            messages,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
            response_format=response_format,
        )

    async def embed(
        self,
        texts: list[str],
        model: str | None = None,
    ) -> list[list[float]]:
        return await self._embedding.embed(texts, model=model)

    async def close(self) -> None:
        for backend in (self._completion, self._embedding):
            close = getattr(backend, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001 — best-effort teardown
                    pass
