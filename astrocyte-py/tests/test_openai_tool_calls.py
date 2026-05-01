"""Tests for native function-calling support in the OpenAI adapter.

Three things this pins down:

1. ``tools`` and ``tool_choice`` are translated to OpenAI's wire format
   correctly (``tools=[{"type": "function", "function": {...}}]``).
2. ``tool_calls`` returned by the API are parsed into ``ToolCall``
   dataclasses with arguments JSON-decoded into dicts.
3. Backward compatibility: the legacy text-only path is unchanged when
   no tools are supplied.

Uses ``unittest.mock`` to replace the ``AsyncOpenAI.chat.completions.create``
coroutine. No real API calls.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from astrocyte.providers.openai import OpenAIProvider
from astrocyte.types import Message, ToolCall, ToolDefinition


def _mock_openai_response(text: str = "", tool_calls: list[dict] | None = None):
    """Build a fake openai-style response object."""
    msg = MagicMock()
    msg.content = text
    if tool_calls is not None:
        oai_tool_calls = []
        for tc in tool_calls:
            tc_obj = MagicMock()
            tc_obj.id = tc["id"]
            tc_obj.function = MagicMock()
            tc_obj.function.name = tc["name"]
            tc_obj.function.arguments = tc["arguments"]
            oai_tool_calls.append(tc_obj)
        msg.tool_calls = oai_tool_calls
    else:
        msg.tool_calls = None

    choice = MagicMock()
    choice.message = msg

    response = MagicMock()
    response.choices = [choice]
    response.model = "gpt-4o-mini"
    response.usage = None
    return response


def _patch_openai_client(provider: OpenAIProvider, response):
    """Replace the provider's underlying client with a stub returning ``response``."""
    provider._client = MagicMock()
    provider._client.chat = MagicMock()
    provider._client.chat.completions = MagicMock()
    provider._client.chat.completions.create = AsyncMock(return_value=response)


class TestToolCallTranslation:
    @pytest.mark.asyncio
    async def test_tools_serialized_to_openai_wire_format(self):
        """``ToolDefinition`` becomes a properly-shaped OpenAI tool dict."""
        provider = OpenAIProvider(api_key="test")
        _patch_openai_client(provider, _mock_openai_response("ok"))

        tool = ToolDefinition(
            name="recall",
            description="Search memory",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        )

        await provider.complete(
            [Message(role="user", content="hi")],
            tools=[tool],
            tool_choice="required",
        )

        kwargs = provider._client.chat.completions.create.call_args.kwargs
        assert kwargs["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "recall",
                    "description": "Search memory",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                },
            }
        ]
        assert kwargs["tool_choice"] == "required"

    @pytest.mark.asyncio
    async def test_tool_choice_specific_function_uses_object_form(self):
        """When ``tool_choice`` is a tool name (not 'auto'/'required'/'none'),
        OpenAI requires the object form ``{'type': 'function', ...}``."""
        provider = OpenAIProvider(api_key="test")
        _patch_openai_client(provider, _mock_openai_response("ok"))

        await provider.complete(
            [Message(role="user", content="hi")],
            tools=[ToolDefinition(name="done", description="x", parameters={})],
            tool_choice="done",
        )

        kwargs = provider._client.chat.completions.create.call_args.kwargs
        assert kwargs["tool_choice"] == {
            "type": "function",
            "function": {"name": "done"},
        }

    @pytest.mark.asyncio
    async def test_no_tools_means_no_tools_kwarg_passed(self):
        """Backward compat: legacy text-only calls must not send a ``tools`` kwarg."""
        provider = OpenAIProvider(api_key="test")
        _patch_openai_client(provider, _mock_openai_response("ok"))

        await provider.complete([Message(role="user", content="hi")])

        kwargs = provider._client.chat.completions.create.call_args.kwargs
        assert "tools" not in kwargs
        assert "tool_choice" not in kwargs


class TestToolCallParsing:
    @pytest.mark.asyncio
    async def test_tool_call_arguments_parsed_to_dict(self):
        """``message.tool_calls[*].function.arguments`` (JSON string) is
        parsed into a dict on the ``ToolCall`` dataclass."""
        provider = OpenAIProvider(api_key="test")
        response = _mock_openai_response(
            text="",
            tool_calls=[
                {
                    "id": "call-1",
                    "name": "recall",
                    "arguments": json.dumps({"query": "Caroline", "max_results": 5}),
                }
            ],
        )
        _patch_openai_client(provider, response)

        completion = await provider.complete(
            [Message(role="user", content="q")],
            tools=[ToolDefinition(name="recall", description="x", parameters={})],
        )

        assert completion.tool_calls is not None
        assert len(completion.tool_calls) == 1
        tc = completion.tool_calls[0]
        assert isinstance(tc, ToolCall)
        assert tc.id == "call-1"
        assert tc.name == "recall"
        assert tc.arguments == {"query": "Caroline", "max_results": 5}

    @pytest.mark.asyncio
    async def test_malformed_arguments_become_empty_dict(self):
        """Defensive: if the model returns invalid JSON in ``arguments``,
        we degrade to an empty dict instead of crashing the loop."""
        provider = OpenAIProvider(api_key="test")
        response = _mock_openai_response(
            text="",
            tool_calls=[{"id": "c", "name": "recall", "arguments": "not json"}],
        )
        _patch_openai_client(provider, response)

        completion = await provider.complete(
            [Message(role="user", content="q")],
            tools=[ToolDefinition(name="recall", description="x", parameters={})],
        )

        assert completion.tool_calls is not None
        assert completion.tool_calls[0].arguments == {}

    @pytest.mark.asyncio
    async def test_no_tool_calls_returns_none(self):
        """Plain text response → ``tool_calls=None``."""
        provider = OpenAIProvider(api_key="test")
        _patch_openai_client(provider, _mock_openai_response("just text"))

        completion = await provider.complete(
            [Message(role="user", content="q")],
            tools=[ToolDefinition(name="recall", description="x", parameters={})],
        )

        assert completion.tool_calls is None
        assert completion.text == "just text"
