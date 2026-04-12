"""Tests for agent framework integrations — LangGraph, CrewAI, Pydantic AI, OpenAI.

All tests use in-memory providers. No framework dependencies required.
"""

import json

import pytest

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.errors import ConfigError
from astrocyte.integrations.crewai import AstrocyteCrewMemory
from astrocyte.integrations.langgraph import AstrocyteMemory
from astrocyte.integrations.openai_agents import astrocyte_tool_definitions
from astrocyte.integrations.pydantic_ai import astrocyte_tools
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
# LangGraph integration
# ---------------------------------------------------------------------------


class TestLangGraphIntegration:
    async def test_save_and_search(self):
        brain, _ = _make_brain()
        memory = AstrocyteMemory(brain, bank_id="user-1")

        await memory.save_context(
            inputs={"question": "What is dark mode?"},
            outputs={"answer": "A UI theme with dark background"},
        )

        results = await memory.search("dark mode")
        assert len(results) >= 1
        assert any("dark" in r["text"].lower() for r in results)

    async def test_load_memory_variables(self):
        brain, _ = _make_brain()
        memory = AstrocyteMemory(brain, bank_id="user-1")

        await memory.save_context(
            inputs={"topic": "Python preferences"},
            outputs={"result": "Calvin prefers Python 3.11"},
        )

        variables = await memory.load_memory_variables({"topic": "Python"})
        assert "memory" in variables
        assert len(variables["memory"]) > 0

    async def test_load_memory_empty(self):
        brain, _ = _make_brain()
        memory = AstrocyteMemory(brain, bank_id="empty-bank")

        variables = await memory.load_memory_variables({"topic": "nothing"})
        assert variables["memory"] == ""

    async def test_thread_to_bank_mapping(self):
        brain, engine = _make_brain()
        memory = AstrocyteMemory(
            brain,
            bank_id="default-bank",
            thread_to_bank={"thread-abc": "custom-bank"},
        )

        await memory.save_context(
            inputs={"msg": "thread-specific content"},
            outputs={},
            thread_id="thread-abc",
        )

        # Should be in custom-bank, not default-bank
        assert "custom-bank" in engine._memories
        assert "default-bank" not in engine._memories

    async def test_thread_fallback_to_default(self):
        brain, engine = _make_brain()
        memory = AstrocyteMemory(brain, bank_id="default-bank")

        await memory.save_context(
            inputs={"msg": "no thread mapping"},
            outputs={},
            thread_id="unknown-thread",
        )

        assert "default-bank" in engine._memories

    async def test_tags_applied(self):
        brain, engine = _make_brain()
        memory = AstrocyteMemory(brain, bank_id="b1")

        await memory.save_context(
            inputs={"q": "test"},
            outputs={"a": "result"},
            tags=["custom-tag"],
        )

        mems = engine._memories.get("b1", [])
        assert len(mems) >= 1
        assert "custom-tag" in (mems[0].tags or [])

    async def test_empty_context_not_stored(self):
        brain, engine = _make_brain()
        memory = AstrocyteMemory(brain, bank_id="b1")

        await memory.save_context(inputs={}, outputs={})

        assert "b1" not in engine._memories


# ---------------------------------------------------------------------------
# CrewAI integration
# ---------------------------------------------------------------------------


class TestCrewAIIntegration:
    async def test_save_and_search(self):
        brain, _ = _make_brain()
        memory = AstrocyteCrewMemory(brain, bank_id="crew-bank")

        await memory.save(
            "The deployment uses Kubernetes with Helm charts",
            agent_id="devops-agent",
        )

        results = await memory.search("Kubernetes")
        assert len(results) >= 1
        assert any("Kubernetes" in r["text"] for r in results)

    async def test_per_agent_banks(self):
        brain, engine = _make_brain()
        memory = AstrocyteCrewMemory(
            brain,
            bank_id="crew-shared",
            agent_banks={"agent-a": "bank-a", "agent-b": "bank-b"},
        )

        await memory.save("Agent A content", agent_id="agent-a")
        await memory.save("Agent B content", agent_id="agent-b")
        await memory.save("Shared content")

        assert "bank-a" in engine._memories
        assert "bank-b" in engine._memories
        assert "crew-shared" in engine._memories

    async def test_metadata_includes_source(self):
        brain, engine = _make_brain()
        memory = AstrocyteCrewMemory(brain, bank_id="b1")

        await memory.save("test content", agent_id="my-agent")

        mems = engine._memories["b1"]
        assert mems[0].metadata is not None
        assert mems[0].metadata.get("source") == "crewai"
        assert mems[0].metadata.get("agent_id") == "my-agent"

    async def test_reset_clears_bank(self):
        brain, engine = _make_brain()
        memory = AstrocyteCrewMemory(brain, bank_id="b1")

        await memory.save("content to clear")
        assert len(engine._memories.get("b1", [])) >= 1

        await memory.reset()
        # After reset, the bank should be empty
        assert len(engine._memories.get("b1", [])) == 0


# ---------------------------------------------------------------------------
# Pydantic AI integration
# ---------------------------------------------------------------------------


class TestPydanticAIIntegration:
    async def test_tools_created(self):
        brain, _ = _make_brain()
        tools = astrocyte_tools(brain, bank_id="b1")

        names = {t["name"] for t in tools}
        assert "memory_retain" in names
        assert "memory_recall" in names
        assert "memory_reflect" in names

    async def test_retain_tool(self):
        brain, engine = _make_brain()
        tools = astrocyte_tools(brain, bank_id="b1")

        retain_fn = next(t["function"] for t in tools if t["name"] == "memory_retain")
        result = await retain_fn("Calvin likes dark mode")
        assert "Stored" in result

        assert len(engine._memories.get("b1", [])) >= 1

    async def test_recall_tool(self):
        brain, _ = _make_brain()
        tools = astrocyte_tools(brain, bank_id="b1")

        retain_fn = next(t["function"] for t in tools if t["name"] == "memory_retain")
        recall_fn = next(t["function"] for t in tools if t["name"] == "memory_recall")

        await retain_fn("Python is Calvin's favorite language")
        result = await recall_fn("favorite language")
        assert "Python" in result

    async def test_reflect_tool(self):
        brain, _ = _make_brain()
        tools = astrocyte_tools(brain, bank_id="b1")

        retain_fn = next(t["function"] for t in tools if t["name"] == "memory_retain")
        reflect_fn = next(t["function"] for t in tools if t["name"] == "memory_reflect")

        await retain_fn("Calvin prefers dark mode and Python")
        result = await reflect_fn("What does Calvin prefer?")
        assert len(result) > 0

    async def test_no_reflect_when_disabled(self):
        brain, _ = _make_brain()
        tools = astrocyte_tools(brain, bank_id="b1", include_reflect=False)
        names = {t["name"] for t in tools}
        assert "memory_reflect" not in names

    async def test_forget_when_enabled(self):
        brain, _ = _make_brain()
        tools = astrocyte_tools(brain, bank_id="b1", include_forget=True)
        names = {t["name"] for t in tools}
        assert "memory_forget" in names

    async def test_recall_no_results(self):
        brain, _ = _make_brain()
        tools = astrocyte_tools(brain, bank_id="empty")
        recall_fn = next(t["function"] for t in tools if t["name"] == "memory_recall")
        result = await recall_fn("anything")
        assert "No relevant memories" in result


# ---------------------------------------------------------------------------
# OpenAI-compatible tools
# ---------------------------------------------------------------------------


class TestOpenAIToolsIntegration:
    async def test_tool_definitions_format(self):
        brain, _ = _make_brain()
        tools, handlers = astrocyte_tool_definitions(brain, bank_id="b1")

        assert len(tools) >= 2
        for tool in tools:
            assert tool["type"] == "function"
            assert "function" in tool
            assert "name" in tool["function"]
            assert "parameters" in tool["function"]

    async def test_handler_names_match_tools(self):
        brain, _ = _make_brain()
        tools, handlers = astrocyte_tool_definitions(brain, bank_id="b1")

        tool_names = {t["function"]["name"] for t in tools}
        handler_names = set(handlers.keys())
        assert tool_names == handler_names

    async def test_retain_handler(self):
        brain, engine = _make_brain()
        _, handlers = astrocyte_tool_definitions(brain, bank_id="b1")

        result_json = await handlers["memory_retain"](content="Test memory")
        result = json.loads(result_json)
        assert result["stored"] is True

    async def test_recall_handler(self):
        brain, _ = _make_brain()
        _, handlers = astrocyte_tool_definitions(brain, bank_id="b1")

        await handlers["memory_retain"](content="Dark mode is preferred")
        result_json = await handlers["memory_recall"](query="dark mode")
        result = json.loads(result_json)
        assert len(result["hits"]) >= 1

    async def test_reflect_handler(self):
        brain, _ = _make_brain()
        _, handlers = astrocyte_tool_definitions(brain, bank_id="b1")

        await handlers["memory_retain"](content="Calvin likes Python")
        result_json = await handlers["memory_reflect"](query="What does Calvin like?")
        result = json.loads(result_json)
        assert result["answer"]

    async def test_reflect_hidden_when_disabled(self):
        brain, _ = _make_brain()
        tools, handlers = astrocyte_tool_definitions(brain, bank_id="b1", include_reflect=False)
        names = {t["function"]["name"] for t in tools}
        assert "memory_reflect" not in names
        assert "memory_reflect" not in handlers

    async def test_forget_when_enabled(self):
        brain, _ = _make_brain()
        tools, handlers = astrocyte_tool_definitions(brain, bank_id="b1", include_forget=True)
        names = {t["function"]["name"] for t in tools}
        assert "memory_forget" in names
        assert "memory_forget" in handlers


# ---------------------------------------------------------------------------
# Error-case tests
# ---------------------------------------------------------------------------


class TestIntegrationErrorCases:
    """Error handling across integrations: retain failure, empty recall, invalid bank_id."""

    async def test_langgraph_search_empty_bank(self):
        brain, _ = _make_brain()
        memory = AstrocyteMemory(brain, bank_id="empty-bank")
        results = await memory.search("anything at all")
        assert results == []

    async def test_langgraph_invalid_bank_id(self):
        brain, _ = _make_brain()
        # Use thread_to_bank to route to an invalid bank_id
        memory = AstrocyteMemory(
            brain,
            bank_id="valid-bank",
            thread_to_bank={"bad-thread": "!!!invalid!!!"},
        )
        with pytest.raises(ConfigError, match="Invalid bank_id"):
            await memory.save_context(
                inputs={"q": "test"},
                outputs={"a": "result"},
                thread_id="bad-thread",
            )

    async def test_crewai_search_empty_bank(self):
        brain, _ = _make_brain()
        memory = AstrocyteCrewMemory(brain, bank_id="empty-bank")
        results = await memory.search("anything")
        assert results == []

    async def test_crewai_invalid_bank_id(self):
        brain, _ = _make_brain()
        memory = AstrocyteCrewMemory(
            brain,
            bank_id="valid-bank",
            agent_banks={"bad-agent": "../traversal"},
        )
        with pytest.raises(ConfigError, match="Invalid bank_id"):
            await memory.save("test", agent_id="bad-agent")

    async def test_pydantic_ai_retain_failure_reports_error(self):
        """Pydantic AI retain tool reports errors in return string."""
        brain, _ = _make_brain()
        # Set a very small max content size to force failure
        brain._config.homeostasis.retain_max_content_bytes = 1
        tools = astrocyte_tools(brain, bank_id="b1")
        retain_fn = next(t["function"] for t in tools if t["name"] == "memory_retain")
        result = await retain_fn("This content is too long to store")
        assert "Failed" in result

    async def test_pydantic_ai_recall_no_results(self):
        brain, _ = _make_brain()
        tools = astrocyte_tools(brain, bank_id="empty-bank")
        recall_fn = next(t["function"] for t in tools if t["name"] == "memory_recall")
        result = await recall_fn("nonexistent topic")
        assert "No relevant memories" in result

    async def test_openai_retain_failure_returns_error_json(self):
        """OpenAI tools retain handler returns error in JSON."""
        brain, _ = _make_brain()
        brain._config.homeostasis.retain_max_content_bytes = 1
        _, handlers = astrocyte_tool_definitions(brain, bank_id="b1")
        result_json = await handlers["memory_retain"](content="Too long content")
        result = json.loads(result_json)
        assert result["stored"] is False
        assert result["error"] is not None

    async def test_openai_recall_empty(self):
        brain, _ = _make_brain()
        _, handlers = astrocyte_tool_definitions(brain, bank_id="empty-bank")
        result_json = await handlers["memory_recall"](query="nothing here")
        result = json.loads(result_json)
        assert result["hits"] == []

    async def test_openai_forget_nonexistent(self):
        brain, _ = _make_brain()
        _, handlers = astrocyte_tool_definitions(brain, bank_id="b1", include_forget=True)
        result_json = await handlers["memory_forget"](memory_ids=["nonexistent-id"])
        result = json.loads(result_json)
        assert result["deleted_count"] == 0
