"""OpenAI-compatible memory wrapper.

This module intentionally targets the common ``client.chat.completions.create``
shape used by OpenAI-compatible and LiteLLM clients while keeping memory policy
inside Astrocyte.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from astrocyte import Astrocyte
from astrocyte.types import AstrocyteContext, MemoryHit


class _CompletionsClient(Protocol):
    async def create(self, **kwargs: Any) -> Any:
        """Create a chat completion."""


def _message_text(message: Any) -> str:
    if isinstance(message, dict):
        content = message.get("content", "")
    else:
        content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
        return "\n".join(p for p in parts if p)
    return str(content)


def _message_role(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("role", "user"))
    return str(getattr(message, "role", "user"))


def _latest_user_text(messages: list[Any]) -> str:
    for message in reversed(messages):
        if _message_role(message) == "user":
            return _message_text(message)
    return _message_text(messages[-1]) if messages else ""


def _assistant_text(response: Any) -> str:
    choices = getattr(response, "choices", None)
    if choices is None and isinstance(response, dict):
        choices = response.get("choices")
    if not choices:
        return ""
    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else getattr(first, "message", None)
    if message is None:
        return ""
    return _message_text(message)


def _memory_message(hits: list[MemoryHit]) -> dict[str, str]:
    if not hits:
        return {"role": "system", "content": "Astrocyte memory: no relevant memories were recalled."}
    lines = ["Astrocyte recalled memory. Use it only when relevant and do not invent beyond it:"]
    for index, hit in enumerate(hits, 1):
        lines.append(f"[{index}] {hit.text}")
    return {"role": "system", "content": "\n".join(lines)}


@dataclass(frozen=True)
class MemoryWrapperConfig:
    """Controls recall injection and post-completion retention."""

    bank_id: str
    context: AstrocyteContext | None = None
    max_recall_results: int = 5
    max_recall_tokens: int | None = 1200
    retain_user_message: bool = True
    retain_assistant_message: bool = True
    retain_source: str = "llm-wrapper"
    retain_tags: list[str] = field(default_factory=lambda: ["llm-wrapper"])


class AstrocyteMemoryWrapper:
    """Decorate a chat completion client with Astrocyte memory."""

    def __init__(
        self,
        completions: _CompletionsClient,
        *,
        brain: Astrocyte,
        config: MemoryWrapperConfig,
    ) -> None:
        self._completions = completions
        self._brain = brain
        self._config = config

    async def create(self, **kwargs: Any) -> Any:
        """Run recall before completion and retain the exchange after completion."""
        messages = list(kwargs.get("messages") or [])
        query = _latest_user_text(messages)

        if query:
            recalled = await self._brain.recall(
                query,
                bank_id=self._config.bank_id,
                max_results=self._config.max_recall_results,
                max_tokens=self._config.max_recall_tokens,
                context=self._config.context,
            )
            kwargs["messages"] = [_memory_message(recalled.hits), *messages]

        response = await self._completions.create(**kwargs)

        retain_parts: list[str] = []
        if query and self._config.retain_user_message:
            retain_parts.append(f"User: {query}")
        answer = _assistant_text(response)
        if answer and self._config.retain_assistant_message:
            retain_parts.append(f"Assistant: {answer}")
        if retain_parts:
            await self._brain.retain(
                "\n".join(retain_parts),
                bank_id=self._config.bank_id,
                context=self._config.context,
                content_type="conversation",
                source=self._config.retain_source,
                tags=list(self._config.retain_tags),
            )

        return response


class _ChatProxy:
    def __init__(self, completions: AstrocyteMemoryWrapper) -> None:
        self.completions = completions


class _ClientProxy:
    def __init__(self, client: Any, completions: AstrocyteMemoryWrapper) -> None:
        self._client = client
        self.chat = _ChatProxy(completions)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


def wrap_openai_client(
    client: Any,
    *,
    brain: Astrocyte,
    bank_id: str,
    context: AstrocyteContext | None = None,
    max_recall_results: int = 5,
    max_recall_tokens: int | None = 1200,
) -> Any:
    """Return a proxy with ``chat.completions.create`` backed by Astrocyte memory."""
    completions = AstrocyteMemoryWrapper(
        client.chat.completions,
        brain=brain,
        config=MemoryWrapperConfig(
            bank_id=bank_id,
            context=context,
            max_recall_results=max_recall_results,
            max_recall_tokens=max_recall_tokens,
        ),
    )
    return _ClientProxy(client, completions)
