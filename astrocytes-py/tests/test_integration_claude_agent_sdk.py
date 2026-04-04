"""Tests for Claude Agent SDK integration.

Tests the no-dependency tool definitions (astrocyte_claude_agent_tools).
The actual SDK MCP server (astrocyte_claude_agent_server) requires
claude_agent_sdk installed and is tested via integration tests.
"""

import json

from astrocytes._astrocyte import Astrocyte
from astrocytes.config import AstrocyteConfig
from astrocytes.integrations.claude_agent_sdk import astrocyte_claude_agent_tools
from astrocytes.testing.in_memory import InMemoryEngineProvider


def _make_brain() -> tuple[Astrocyte, InMemoryEngineProvider]:
    config = AstrocyteConfig()
    config.provider = "test"
    config.barriers.pii.mode = "disabled"
    brain = Astrocyte(config)
    engine = InMemoryEngineProvider()
    brain.set_engine_provider(engine)
    return brain, engine


class TestClaudeAgentSDKTools:
    async def test_tools_created(self):
        brain, _ = _make_brain()
        tools = astrocyte_claude_agent_tools(brain, bank_id="b1")
        names = [t["name"] for t in tools]
        assert "memory_retain" in names
        assert "memory_recall" in names
        assert "memory_reflect" in names

    async def test_tool_has_sdk_fields(self):
        brain, _ = _make_brain()
        tools = astrocyte_claude_agent_tools(brain, bank_id="b1")
        for tool in tools:
            assert "name" in tool
            assert "description" in tool
            assert "input_schema" in tool
            assert "handler" in tool

    async def test_retain_handler_returns_sdk_format(self):
        brain, engine = _make_brain()
        tools = astrocyte_claude_agent_tools(brain, bank_id="b1")
        retain = next(t for t in tools if t["name"] == "memory_retain")

        result = await retain["handler"]({"content": "Claude SDK test memory"})

        # SDK format: {"content": [{"type": "text", "text": "..."}]}
        assert "content" in result
        assert isinstance(result["content"], list)
        assert result["content"][0]["type"] == "text"

        data = json.loads(result["content"][0]["text"])
        assert data["stored"] is True
        assert data["memory_id"] is not None

    async def test_recall_handler_returns_sdk_format(self):
        brain, _ = _make_brain()
        tools = astrocyte_claude_agent_tools(brain, bank_id="b1")
        retain = next(t for t in tools if t["name"] == "memory_retain")
        recall = next(t for t in tools if t["name"] == "memory_recall")

        await retain["handler"]({"content": "Python is great for AI development"})
        result = await recall["handler"]({"query": "Python"})

        assert "content" in result
        data = json.loads(result["content"][0]["text"])
        assert len(data["hits"]) >= 1
        assert "Python" in data["hits"][0]["text"]

    async def test_reflect_handler_returns_sdk_format(self):
        brain, _ = _make_brain()
        tools = astrocyte_claude_agent_tools(brain, bank_id="b1")
        retain = next(t for t in tools if t["name"] == "memory_retain")
        reflect = next(t for t in tools if t["name"] == "memory_reflect")

        await retain["handler"]({"content": "Calvin likes dark mode and Python"})
        result = await reflect["handler"]({"query": "What does Calvin like?"})

        assert "content" in result
        assert result["content"][0]["type"] == "text"
        assert len(result["content"][0]["text"]) > 0

    async def test_no_reflect_when_disabled(self):
        brain, _ = _make_brain()
        tools = astrocyte_claude_agent_tools(brain, bank_id="b1", include_reflect=False)
        names = [t["name"] for t in tools]
        assert "memory_reflect" not in names

    async def test_forget_when_enabled(self):
        brain, _ = _make_brain()
        tools = astrocyte_claude_agent_tools(brain, bank_id="b1", include_forget=True)
        names = [t["name"] for t in tools]
        assert "memory_forget" in names

    async def test_forget_handler(self):
        brain, _ = _make_brain()
        tools = astrocyte_claude_agent_tools(brain, bank_id="b1", include_forget=True)
        retain = next(t for t in tools if t["name"] == "memory_retain")
        forget = next(t for t in tools if t["name"] == "memory_forget")

        retain_result = await retain["handler"]({"content": "to delete"})
        mem_id = json.loads(retain_result["content"][0]["text"])["memory_id"]

        forget_result = await forget["handler"]({"memory_ids": [mem_id]})
        data = json.loads(forget_result["content"][0]["text"])
        assert data["deleted_count"] >= 1

    async def test_retain_with_comma_tags(self):
        brain, engine = _make_brain()
        tools = astrocyte_claude_agent_tools(brain, bank_id="b1")
        retain = next(t for t in tools if t["name"] == "memory_retain")

        await retain["handler"]({"content": "tagged", "tags": "pref,ui"})
        mems = engine._memories.get("b1", [])
        assert mems[0].tags == ["pref", "ui"]
