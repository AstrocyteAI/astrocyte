"""Tests for Claude Managed Agents integration.

Tests tool definitions, the memory tool handler, and the is_memory_tool check.
No anthropic SDK dependency required — tests the pure logic layer.
"""

import json

import pytest

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.integrations.claude_managed_agents import (
    MEMORY_FORGET,
    MEMORY_RECALL,
    MEMORY_REFLECT,
    MEMORY_RETAIN,
    _parse_tags,
    handle_memory_tool,
    is_memory_tool,
    memory_tool_definitions,
)
from astrocyte.testing.in_memory import InMemoryEngineProvider


def _make_brain() -> tuple[Astrocyte, InMemoryEngineProvider]:
    config = AstrocyteConfig()
    config.provider = "test"
    config.barriers.pii.mode = "disabled"
    brain = Astrocyte(config)
    engine = InMemoryEngineProvider()
    brain.set_engine_provider(engine)
    return brain, engine


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


class TestToolDefinitions:
    def test_default_tools(self):
        tools = memory_tool_definitions()
        names = [t["name"] for t in tools]
        assert names == ["memory_retain", "memory_recall", "memory_reflect"]

    def test_all_have_custom_type(self):
        tools = memory_tool_definitions(include_forget=True)
        for t in tools:
            assert t["type"] == "custom"

    def test_all_have_input_schema(self):
        tools = memory_tool_definitions(include_forget=True)
        for t in tools:
            schema = t["input_schema"]
            assert schema["type"] == "object"
            assert "properties" in schema
            assert "required" in schema

    def test_without_reflect(self):
        tools = memory_tool_definitions(include_reflect=False)
        names = [t["name"] for t in tools]
        assert "memory_reflect" not in names
        assert "memory_retain" in names
        assert "memory_recall" in names

    def test_with_forget(self):
        tools = memory_tool_definitions(include_forget=True)
        names = [t["name"] for t in tools]
        assert "memory_forget" in names

    def test_retain_schema_has_content_required(self):
        tools = memory_tool_definitions()
        retain = next(t for t in tools if t["name"] == "memory_retain")
        assert "content" in retain["input_schema"]["required"]
        assert "content" in retain["input_schema"]["properties"]

    def test_recall_schema_has_query_required(self):
        tools = memory_tool_definitions()
        recall = next(t for t in tools if t["name"] == "memory_recall")
        assert "query" in recall["input_schema"]["required"]

    def test_reflect_schema_has_query_required(self):
        tools = memory_tool_definitions()
        reflect = next(t for t in tools if t["name"] == "memory_reflect")
        assert "query" in reflect["input_schema"]["required"]


# ---------------------------------------------------------------------------
# is_memory_tool
# ---------------------------------------------------------------------------


class TestIsMemoryTool:
    def test_known_tools(self):
        assert is_memory_tool("memory_retain") is True
        assert is_memory_tool("memory_recall") is True
        assert is_memory_tool("memory_reflect") is True
        assert is_memory_tool("memory_forget") is True

    def test_unknown_tool(self):
        assert is_memory_tool("bash") is False
        assert is_memory_tool("read") is False
        assert is_memory_tool("memory_something") is False

    def test_constants_match(self):
        assert MEMORY_RETAIN == "memory_retain"
        assert MEMORY_RECALL == "memory_recall"
        assert MEMORY_REFLECT == "memory_reflect"
        assert MEMORY_FORGET == "memory_forget"


# ---------------------------------------------------------------------------
# Tag parsing
# ---------------------------------------------------------------------------


class TestParseTags:
    def test_comma_separated(self):
        assert _parse_tags("a, b, c") == ["a", "b", "c"]

    def test_single_tag(self):
        assert _parse_tags("only") == ["only"]

    def test_empty_string(self):
        assert _parse_tags("") is None

    def test_whitespace_only(self):
        assert _parse_tags("   ") is None

    def test_none(self):
        assert _parse_tags(None) is None

    def test_strips_empty_entries(self):
        assert _parse_tags("a,,b") == ["a", "b"]


# ---------------------------------------------------------------------------
# handle_memory_tool — retain
# ---------------------------------------------------------------------------


class TestHandleRetain:
    async def test_stores_content(self):
        brain, engine = _make_brain()
        result_str = await handle_memory_tool(
            brain, "memory_retain",
            {"content": "Calvin prefers dark mode"},
            bank_id="test-bank",
        )
        data = json.loads(result_str)
        assert data["stored"] is True
        assert data["memory_id"] is not None
        mems = engine._memories.get("test-bank", [])
        assert len(mems) == 1
        assert "dark mode" in mems[0].text

    async def test_stores_with_tags(self):
        brain, engine = _make_brain()
        await handle_memory_tool(
            brain, "memory_retain",
            {"content": "tagged content", "tags": "pref,ui"},
            bank_id="test-bank",
        )
        mems = engine._memories.get("test-bank", [])
        assert mems[0].tags == ["pref", "ui"]

    async def test_stores_without_tags(self):
        brain, _ = _make_brain()
        result_str = await handle_memory_tool(
            brain, "memory_retain",
            {"content": "no tags"},
            bank_id="test-bank",
        )
        data = json.loads(result_str)
        assert data["stored"] is True


# ---------------------------------------------------------------------------
# handle_memory_tool — recall
# ---------------------------------------------------------------------------


class TestHandleRecall:
    async def test_recall_finds_stored_content(self):
        brain, _ = _make_brain()
        await brain.retain("Python is my favorite language", bank_id="test-bank")

        result_str = await handle_memory_tool(
            brain, "memory_recall",
            {"query": "favorite language"},
            bank_id="test-bank",
        )
        data = json.loads(result_str)
        assert len(data["hits"]) >= 1
        assert "Python" in data["hits"][0]["text"]

    async def test_recall_empty_bank(self):
        brain, _ = _make_brain()
        result_str = await handle_memory_tool(
            brain, "memory_recall",
            {"query": "anything"},
            bank_id="empty-bank",
        )
        data = json.loads(result_str)
        assert data["hits"] == []
        assert data["total"] == 0

    async def test_recall_respects_max_results(self):
        brain, _ = _make_brain()
        for i in range(5):
            await brain.retain(f"Fact number {i}", bank_id="test-bank")

        result_str = await handle_memory_tool(
            brain, "memory_recall",
            {"query": "fact", "max_results": 2},
            bank_id="test-bank",
        )
        data = json.loads(result_str)
        assert len(data["hits"]) <= 2

    async def test_recall_default_max_results(self):
        brain, _ = _make_brain()
        # Default max_results should be 5
        result_str = await handle_memory_tool(
            brain, "memory_recall",
            {"query": "anything"},
            bank_id="test-bank",
        )
        data = json.loads(result_str)
        assert isinstance(data["hits"], list)


# ---------------------------------------------------------------------------
# handle_memory_tool — reflect
# ---------------------------------------------------------------------------


class TestHandleReflect:
    async def test_reflect_returns_answer(self):
        brain, _ = _make_brain()
        await brain.retain("Calvin prefers dark mode", bank_id="test-bank")

        result_str = await handle_memory_tool(
            brain, "memory_reflect",
            {"query": "What does Calvin prefer?"},
            bank_id="test-bank",
        )
        # Reflect returns plain text, not JSON
        assert len(result_str) > 0


# ---------------------------------------------------------------------------
# handle_memory_tool — forget
# ---------------------------------------------------------------------------


class TestHandleForget:
    async def test_forget_deletes(self):
        brain, engine = _make_brain()
        retain_result = await brain.retain("to be deleted", bank_id="test-bank")

        result_str = await handle_memory_tool(
            brain, "memory_forget",
            {"memory_ids": retain_result.memory_id},
            bank_id="test-bank",
        )
        data = json.loads(result_str)
        assert data["deleted_count"] >= 0


# ---------------------------------------------------------------------------
# handle_memory_tool — unknown tool
# ---------------------------------------------------------------------------


class TestHandleUnknownTool:
    async def test_raises_value_error(self):
        brain, _ = _make_brain()
        with pytest.raises(ValueError, match="Unknown memory tool"):
            await handle_memory_tool(
                brain, "unknown_tool", {}, bank_id="test-bank"
            )


# ---------------------------------------------------------------------------
# Bank isolation across tools
# ---------------------------------------------------------------------------


class TestBankIsolation:
    async def test_different_banks_isolated(self):
        brain, _ = _make_brain()
        await handle_memory_tool(
            brain, "memory_retain",
            {"content": "bank A secret"},
            bank_id="bank-a",
        )

        result_str = await handle_memory_tool(
            brain, "memory_recall",
            {"query": "bank A secret"},
            bank_id="bank-b",
        )
        data = json.loads(result_str)
        assert len(data["hits"]) == 0

    async def test_same_bank_accessible(self):
        brain, _ = _make_brain()
        await handle_memory_tool(
            brain, "memory_retain",
            {"content": "shared fact about Python"},
            bank_id="shared",
        )

        result_str = await handle_memory_tool(
            brain, "memory_recall",
            {"query": "Python"},
            bank_id="shared",
        )
        data = json.loads(result_str)
        assert len(data["hits"]) >= 1
