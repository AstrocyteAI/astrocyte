"""OpenAI LLMProvider adapter.

Wraps the openai Python SDK to implement the Astrocyte LLMProvider protocol.

Usage via config YAML:
    llm_provider: openai
    llm_provider_config:
      api_key: ${OPENAI_API_KEY}
      model: gpt-4o-mini
      embedding_model: text-embedding-3-small

Usage programmatically:
    from astrocyte.providers.openai import OpenAIProvider

    llm = OpenAIProvider(api_key="sk-...", model="gpt-4o-mini")
"""

from __future__ import annotations

import os
from typing import ClassVar

from astrocyte.types import (
    Completion,
    ContentPart,
    LLMCapabilities,
    Message,
    TokenUsage,
)


class OpenAIProvider:
    """LLMProvider backed by the OpenAI API."""

    SPI_VERSION: ClassVar[int] = 1

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "gpt-4o-mini",
        embedding_model: str = "text-embedding-3-small",
        base_url: str | None = None,
    ) -> None:
        try:
            import openai
        except ImportError as e:
            raise ImportError(
                "The 'openai' package is required for OpenAIProvider. "
                "Install it with: pip install 'astrocyte[openai]'"
            ) from e

        resolved_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not resolved_key:
            raise ValueError(
                "OpenAI API key is required. Pass api_key= or set OPENAI_API_KEY."
            )

        self._client = openai.AsyncOpenAI(api_key=resolved_key, base_url=base_url)
        self._model = model
        self._embedding_model = embedding_model

    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities(
            supports_multimodal_completion=True,
            modalities_supported=("text", "image_url", "image_base64"),
            supports_multimodal_embedding=False,
            supports_batch_embed=True,
        )

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> Completion:
        oai_messages = [_to_oai_message(m) for m in messages]
        use_model = model or self._model

        response = await self._client.chat.completions.create(
            model=use_model,
            messages=oai_messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )

        choice = response.choices[0]
        usage = None
        if response.usage:
            usage = TokenUsage(
                input_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
            )

        return Completion(
            text=choice.message.content or "",
            model=response.model,
            usage=usage,
        )

    async def embed(
        self,
        texts: list[str],
        model: str | None = None,
    ) -> list[list[float]]:
        use_model = model or self._embedding_model

        response = await self._client.embeddings.create(
            model=use_model,
            input=texts,
        )

        # Sort by index to guarantee order matches input
        sorted_data = sorted(response.data, key=lambda d: d.index)
        return [d.embedding for d in sorted_data]


def _to_oai_message(msg: Message) -> dict:
    """Convert an Astrocyte Message to an OpenAI API message dict."""
    if isinstance(msg.content, str):
        return {"role": msg.role, "content": msg.content}

    # Multimodal: list of ContentPart
    parts = []
    for part in msg.content:
        if part.type == "text" and part.text:
            parts.append({"type": "text", "text": part.text})
        elif part.type == "image_url" and part.image_url:
            parts.append({
                "type": "image_url",
                "image_url": {"url": part.image_url},
            })
        elif part.type == "image_base64" and part.image_base64:
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{part.image_base64}"},
            })

    return {"role": msg.role, "content": parts}
