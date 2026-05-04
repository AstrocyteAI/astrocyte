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

import json
import os
from typing import ClassVar

from astrocyte.types import (
    Completion,
    LLMCapabilities,
    Message,
    TokenUsage,
    ToolCall,
    ToolDefinition,
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
                "The 'openai' package is required for OpenAIProvider. Install it with: pip install 'astrocyte[openai]'"
            ) from e

        resolved_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not resolved_key:
            raise ValueError("OpenAI API key is required. Pass api_key= or set OPENAI_API_KEY.")

        # Use HTTP/2 multiplexing when ``h2`` is installed. Without it, the
        # OpenAI SDK's default httpx client falls back to HTTP/1.1, which can
        # only carry one in-flight request per TCP connection — a hard
        # serialisation bottleneck for concurrent retain/recall workloads.
        # With HTTP/2, a single connection multiplexes dozens of requests
        # (typical 10-30x throughput improvement on embedding/completion
        # heavy phases). On benchmark hardware retaining 272 LoCoMo sessions
        # drops from ~6h on HTTP/1.1 to under a minute with HTTP/2.
        try:
            import h2  # noqa: F401  — presence enables httpx HTTP/2
            import httpx

            http_client: httpx.AsyncClient | None = httpx.AsyncClient(
                http2=True,
                # Match OpenAI's defaults but with explicit pool settings so
                # we don't suddenly serialise on bursty workloads.
                limits=httpx.Limits(
                    max_connections=100,
                    max_keepalive_connections=20,
                    keepalive_expiry=30.0,
                ),
                # Read timeout was 600s historically — far too long when an
                # HTTP/2 stream stalls (no RST/FIN, just no bytes). Such
                # stalls block for the full timeout × ``max_retries`` (≈30min
                # per stuck call) and silently murder benchmark wall-clock.
                # OpenAI completions normally take 5-30s; 90s is generous
                # while still failing fast on stuck streams. ``max_retries=3``
                # below handles transient stalls.
                timeout=httpx.Timeout(connect=5.0, read=90.0, write=90.0, pool=90.0),
            )
        except ImportError:
            # h2 not installed — let the OpenAI SDK use its default HTTP/1.1
            # client. Throughput will be lower for concurrent workloads but
            # functionality is unchanged.
            http_client = None

        client_kwargs: dict[str, object] = {
            "api_key": resolved_key,
            "base_url": base_url,
            "max_retries": 3,  # Retry on 429 / 5xx with exponential backoff
        }
        if http_client is not None:
            client_kwargs["http_client"] = http_client

        self._client = openai.AsyncOpenAI(**client_kwargs)
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
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = None,
    ) -> Completion:
        oai_messages = [_to_oai_message(m) for m in messages]
        use_model = model or self._model

        # Native function-calling pass-through (Hindsight parity).
        # When ``tools`` is set, translate to OpenAI's wire format and
        # forward; otherwise the request is exactly as before so the
        # legacy text-only callers see no change.
        kwargs: dict = {
            "model": use_model,
            "messages": oai_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
            # OpenAI accepts "auto" / "required" / "none" / specific function ref.
            if tool_choice in ("auto", "required", "none"):
                kwargs["tool_choice"] = tool_choice
            elif tool_choice:
                kwargs["tool_choice"] = {"type": "function", "function": {"name": tool_choice}}
            else:
                kwargs["tool_choice"] = "auto"

        response = await self._client.chat.completions.create(**kwargs)

        choice = response.choices[0]
        usage = None
        if response.usage:
            usage = TokenUsage(
                input_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
            )

        # Parse tool calls when the model emitted them. The OpenAI SDK
        # returns ``message.tool_calls`` as a list (or ``None``); each
        # item has ``id``, ``function.name``, ``function.arguments`` (a
        # JSON string we parse to a dict).
        tool_calls: list[ToolCall] | None = None
        raw_tool_calls = getattr(choice.message, "tool_calls", None)
        if raw_tool_calls:
            tool_calls = []
            for tc in raw_tool_calls:
                fn = getattr(tc, "function", None)
                if fn is None:
                    continue
                try:
                    args = json.loads(fn.arguments) if fn.arguments else {}
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=fn.name,
                        arguments=args if isinstance(args, dict) else {},
                    )
                )

        return Completion(
            text=choice.message.content or "",
            model=response.model,
            usage=usage,
            tool_calls=tool_calls,
        )

    async def embed(
        self,
        texts: list[str],
        model: str | None = None,
    ) -> list[list[float]]:
        use_model = model or self._embedding_model

        # Truncate texts that would exceed the model's token limit.
        # text-embedding-3-small has an 8192-token limit (~30K chars).
        # Truncating at 28K chars provides a safe margin.
        max_chars = 28_000
        safe_texts = [_sanitize_text(t)[:max_chars] for t in texts]

        response = await self._client.embeddings.create(
            model=use_model,
            input=safe_texts,
        )

        # Sort by index to guarantee order matches input
        sorted_data = sorted(response.data, key=lambda d: d.index)
        return [d.embedding for d in sorted_data]


def _sanitize_text(text: str) -> str:
    """Remove control characters that break the OpenAI API's JSON parser."""
    # Remove null bytes and other C0 control chars except \t \n \r
    return "".join(ch for ch in text if ch in ("\t", "\n", "\r") or (ord(ch) >= 32))


def _to_oai_message(msg: Message) -> dict:
    """Convert an Astrocyte Message to an OpenAI API message dict.

    Tool-call round-trip support (Hindsight-parity agentic reflect):
    - ``role="tool"`` messages carry ``tool_call_id`` (required by
      OpenAI) and the tool's serialized output as ``content``.
    - ``role="assistant"`` messages with ``tool_calls`` are translated
      back into the OpenAI ``tool_calls`` array so the model sees its
      own prior calls when the loop continues.
    """
    if msg.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": msg.tool_call_id or "",
            "content": _sanitize_text(msg.content) if isinstance(msg.content, str) else "",
        }
    if msg.role == "assistant" and msg.tool_calls:
        return {
            "role": "assistant",
            "content": _sanitize_text(msg.content) if isinstance(msg.content, str) else None,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments),
                    },
                }
                for tc in msg.tool_calls
            ],
        }
    if isinstance(msg.content, str):
        return {"role": msg.role, "content": _sanitize_text(msg.content)}

    # Multimodal: list of ContentPart
    parts = []
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
                    "image_url": {"url": f"data:image/png;base64,{part.image_base64}"},
                }
            )

    return {"role": msg.role, "content": parts}
