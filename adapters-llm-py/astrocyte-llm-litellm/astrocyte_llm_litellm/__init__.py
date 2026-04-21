"""LiteLLM LLMProvider adapter for Astrocyte.

Wraps the litellm library to implement the Astrocyte LLMProvider protocol,
giving access to 100+ models across all major providers through a single adapter.

Usage via config YAML:
    llm_provider: litellm
    llm_provider_config:
      model: openai/gpt-4o-mini
      embedding_model: openai/text-embedding-3-small

    # AWS Bedrock
    llm_provider: litellm
    llm_provider_config:
      model: bedrock/anthropic.claude-sonnet-4-20250514-v1:0

    # Self-hosted (Ollama, vLLM, LM Studio)
    llm_provider: litellm
    llm_provider_config:
      model: ollama/llama3.2
      api_base: http://localhost:11434

Usage programmatically:
    from astrocyte_llm_litellm import LiteLLMProvider

    llm = LiteLLMProvider(model="openai/gpt-4o-mini")
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from astrocyte.types import (
    Completion,
    LLMCapabilities,
    Message,
    TokenUsage,
)

__all__ = ["LiteLLMProvider"]

_logger = logging.getLogger(__name__)

# Characters per token estimate for safe truncation (conservative).
_CHARS_PER_TOKEN = 3
_DEFAULT_EMBED_TOKEN_LIMIT = 8192
_MAX_EMBED_CHARS = _DEFAULT_EMBED_TOKEN_LIMIT * _CHARS_PER_TOKEN


class LiteLLMProvider:
    """LLMProvider backed by LiteLLM — 100+ models across all major providers.

    LiteLLM handles provider-specific request/response translation internally.
    This adapter maps Astrocyte's ``complete()`` and ``embed()`` protocol to
    ``litellm.acompletion()`` and ``litellm.aembedding()``.
    """

    SPI_VERSION: ClassVar[int] = 1

    def __init__(
        self,
        *,
        model: str = "openai/gpt-4o-mini",
        embedding_model: str = "openai/text-embedding-3-small",
        api_key: str | None = None,
        api_base: str | None = None,
        max_retries: int = 3,
        timeout: float = 60.0,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Initialise the LiteLLM adapter.

        Args:
            model: LiteLLM model identifier for completions
                (e.g. ``openai/gpt-4o``, ``bedrock/anthropic.claude-sonnet-4-20250514-v1:0``,
                ``ollama/llama3.2``).
            embedding_model: LiteLLM model identifier for embeddings.
            api_key: Optional API key (falls back to provider-specific env vars).
            api_base: Optional base URL for self-hosted or proxied endpoints.
            max_retries: Retry count on transient failures (429, 5xx).
            timeout: Request timeout in seconds.
            extra: Additional keyword arguments forwarded to every litellm call
                (e.g. ``vertex_project``, ``vertex_location``, ``aws_region_name``).
        """
        try:
            import litellm  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "The 'litellm' package is required for LiteLLMProvider. "
                "Install it with: pip install astrocyte-llm-litellm"
            ) from e

        self._model = model
        self._embedding_model = embedding_model
        self._api_key = api_key
        self._api_base = api_base
        self._max_retries = max_retries
        self._timeout = timeout
        self._extra: dict[str, Any] = extra or {}

    def capabilities(self) -> LLMCapabilities:
        """Declare capabilities.

        LiteLLM supports multimodal for providers that offer it (OpenAI, Anthropic,
        Gemini, etc.). We declare support; litellm raises if the underlying model
        does not actually handle the modality.
        """
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
        """Generate a chat completion via LiteLLM."""
        import litellm

        litellm_messages = [_to_litellm_message(m) for m in messages]
        use_model = model or self._model

        kwargs: dict[str, Any] = {
            "model": use_model,
            "messages": litellm_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "num_retries": self._max_retries,
            "timeout": self._timeout,
            **self._extra,
        }
        if self._api_key:
            kwargs["api_key"] = self._api_key
        if self._api_base:
            kwargs["api_base"] = self._api_base

        response = await litellm.acompletion(**kwargs)

        choice = response.choices[0]
        usage = None
        if response.usage:
            usage = TokenUsage(
                input_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
            )

        return Completion(
            text=choice.message.content or "",
            model=response.model or use_model,
            usage=usage,
        )

    async def embed(
        self,
        texts: list[str],
        model: str | None = None,
    ) -> list[list[float]]:
        """Generate embedding vectors via LiteLLM."""
        import litellm

        use_model = model or self._embedding_model

        # Truncate to stay within typical embedding model token limits.
        # Log when truncation actually happens so operators can see silent
        # clipping in embedding inputs (previously invisible; CodeQL flagged
        # the unused _logger that this closes).
        safe_texts: list[str] = []
        truncated_count = 0
        for t in texts:
            sanitized = _sanitize_text(t)
            if len(sanitized) > _MAX_EMBED_CHARS:
                truncated_count += 1
                sanitized = sanitized[:_MAX_EMBED_CHARS]
            safe_texts.append(sanitized)
        if truncated_count:
            _logger.warning(
                "LiteLLMProvider.embed truncated %d of %d input(s) to %d chars "
                "(model=%s). Longer inputs risk provider-side rejection; "
                "consider pre-chunking upstream.",
                truncated_count, len(texts), _MAX_EMBED_CHARS, use_model,
            )

        kwargs: dict[str, Any] = {
            "model": use_model,
            "input": safe_texts,
            "num_retries": self._max_retries,
            "timeout": self._timeout,
            **self._extra,
        }
        if self._api_key:
            kwargs["api_key"] = self._api_key
        if self._api_base:
            kwargs["api_base"] = self._api_base

        response = await litellm.aembedding(**kwargs)

        # Sort by index to guarantee order matches input.
        sorted_data = sorted(response.data, key=lambda d: d["index"])
        return [d["embedding"] for d in sorted_data]


def _sanitize_text(text: str) -> str:
    """Remove control characters that break JSON parsers across providers."""
    return "".join(ch for ch in text if ch in ("\t", "\n", "\r") or (ord(ch) >= 32))


def _to_litellm_message(msg: Message) -> dict[str, Any]:
    """Convert an Astrocyte Message to a LiteLLM/OpenAI-style message dict."""
    if isinstance(msg.content, str):
        return {"role": msg.role, "content": _sanitize_text(msg.content)}

    # Multimodal: list of ContentPart → OpenAI-style content parts.
    # LiteLLM forwards these to the provider in the appropriate format.
    parts: list[dict[str, Any]] = []
    for part in msg.content:
        if part.type == "text" and part.text:
            parts.append({"type": "text", "text": _sanitize_text(part.text)})
        elif part.type == "image_url" and part.image_url:
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": part.image_url},
                }
            )
        elif part.type == "image_base64" and part.image_base64:
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{part.media_type or 'image/png'};base64,{part.image_base64}"},
                }
            )

    return {"role": msg.role, "content": parts}
