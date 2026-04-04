"""Tests for extended agent framework integrations — Google ADK, AutoGen, Smolagents, LlamaIndex, Strands."""

import json

from astrocytes._astrocyte import Astrocyte
from astrocytes.config import AstrocyteConfig
from astrocytes.integrations.autogen import AstrocyteAutoGenMemory
from astrocytes.integrations.google_adk import astrocyte_adk_tools
from astrocytes.integrations.llamaindex import AstrocyteLlamaMemory
from astrocytes.integrations.smolagents import AstrocyteSmolTool, astrocyte_smolagent_tools
from astrocytes.integrations.strands import astrocyte_strands_tools
from astrocytes.testing.in_memory import InMemoryEngineProvider


def _make_brain() -> tuple[Astrocyte, InMemoryEngineProvider]:
    config = AstrocyteConfig()
    config.provider = "test"
    config.barriers.pii.mode = "disabled"
    brain = Astrocyte(config)
    engine = InMemoryEngineProvider()
    brain.set_engine_provider(engine)
    return brain, engine


# ---------------------------------------------------------------------------
# Google ADK
# ---------------------------------------------------------------------------


class TestGoogleADK:
    async def test_tools_created(self):
        brain, _ = _make_brain()
        tools = astrocyte_adk_tools(brain, bank_id="b1")
        names = [t.__name__ for t in tools]
        assert "memory_retain" in names
        assert "memory_recall" in names
        assert "memory_reflect" in names

    async def test_retain_tool(self):
        brain, engine = _make_brain()
        tools = astrocyte_adk_tools(brain, bank_id="b1")
        retain = next(t for t in tools if t.__name__ == "memory_retain")

        result = await retain(content="ADK test memory")
        assert result["stored"] is True
        assert result["memory_id"] is not None

    async def test_recall_tool(self):
        brain, _ = _make_brain()
        tools = astrocyte_adk_tools(brain, bank_id="b1")
        retain = next(t for t in tools if t.__name__ == "memory_retain")
        recall = next(t for t in tools if t.__name__ == "memory_recall")

        await retain(content="Python is great for AI")
        result = await recall(query="Python")
        assert len(result["hits"]) >= 1

    async def test_reflect_tool(self):
        brain, _ = _make_brain()
        tools = astrocyte_adk_tools(brain, bank_id="b1")
        retain = next(t for t in tools if t.__name__ == "memory_retain")
        reflect = next(t for t in tools if t.__name__ == "memory_reflect")

        await retain(content="Calvin likes dark mode")
        result = await reflect(query="What does Calvin like?")
        assert result["answer"]

    async def test_no_reflect_when_disabled(self):
        brain, _ = _make_brain()
        tools = astrocyte_adk_tools(brain, bank_id="b1", include_reflect=False)
        names = [t.__name__ for t in tools]
        assert "memory_reflect" not in names

    async def test_retain_with_tags(self):
        brain, engine = _make_brain()
        tools = astrocyte_adk_tools(brain, bank_id="b1")
        retain = next(t for t in tools if t.__name__ == "memory_retain")

        await retain(content="Tagged content", tags="pref,ui")
        mems = engine._memories.get("b1", [])
        assert mems[0].tags == ["pref", "ui"]


# ---------------------------------------------------------------------------
# AutoGen / AG2
# ---------------------------------------------------------------------------


class TestAutoGen:
    async def test_save_and_query(self):
        brain, _ = _make_brain()
        memory = AstrocyteAutoGenMemory(brain, bank_id="team")

        mem_id = await memory.save("Kubernetes deploys via Helm", agent_id="devops")
        assert mem_id is not None

        results = await memory.query("Kubernetes")
        assert len(results) >= 1
        assert any("Kubernetes" in r["text"] for r in results)

    async def test_get_context(self):
        brain, _ = _make_brain()
        memory = AstrocyteAutoGenMemory(brain, bank_id="b1")

        await memory.save("Calvin prefers dark mode")
        context = await memory.get_context("dark mode")
        assert "dark mode" in context

    async def test_get_context_empty(self):
        brain, _ = _make_brain()
        memory = AstrocyteAutoGenMemory(brain, bank_id="empty")
        context = await memory.get_context("anything")
        assert context == ""

    async def test_per_agent_banks(self):
        brain, engine = _make_brain()
        memory = AstrocyteAutoGenMemory(
            brain,
            bank_id="shared",
            agent_banks={"agent-a": "bank-a"},
        )

        await memory.save("Agent A data", agent_id="agent-a")
        await memory.save("Shared data")

        assert "bank-a" in engine._memories
        assert "shared" in engine._memories

    async def test_as_tools(self):
        brain, _ = _make_brain()
        memory = AstrocyteAutoGenMemory(brain, bank_id="b1")
        tools = memory.as_tools()
        assert len(tools) >= 2
        assert all(t["type"] == "function" for t in tools)

    async def test_get_handlers(self):
        brain, _ = _make_brain()
        memory = AstrocyteAutoGenMemory(brain, bank_id="b1")
        handlers = memory.get_handlers()
        assert "memory_retain" in handlers
        assert "memory_recall" in handlers


# ---------------------------------------------------------------------------
# Smolagents (HuggingFace)
# ---------------------------------------------------------------------------


class TestSmolagents:
    async def test_tools_created(self):
        brain, _ = _make_brain()
        tools = astrocyte_smolagent_tools(brain, bank_id="b1")
        names = [t.name for t in tools]
        assert "memory_retain" in names
        assert "memory_recall" in names
        assert "memory_reflect" in names

    async def test_tool_has_protocol_fields(self):
        brain, _ = _make_brain()
        tools = astrocyte_smolagent_tools(brain, bank_id="b1")
        retain = tools[0]

        assert isinstance(retain, AstrocyteSmolTool)
        assert retain.name == "memory_retain"
        assert retain.description
        assert retain.inputs
        assert retain.output_type == "string"

    async def test_retain_via_forward(self):
        brain, engine = _make_brain()
        tools = astrocyte_smolagent_tools(brain, bank_id="b1")
        retain = next(t for t in tools if t.name == "memory_retain")

        result = await retain.forward(content="Smolagent memory test")
        assert "Stored" in result
        assert len(engine._memories.get("b1", [])) >= 1

    async def test_recall_via_forward(self):
        brain, _ = _make_brain()
        tools = astrocyte_smolagent_tools(brain, bank_id="b1")
        retain = next(t for t in tools if t.name == "memory_retain")
        recall = next(t for t in tools if t.name == "memory_recall")

        await retain.forward(content="Python for machine learning")
        result = await recall.forward(query="Python")
        assert "Python" in result

    async def test_no_reflect_when_disabled(self):
        brain, _ = _make_brain()
        tools = astrocyte_smolagent_tools(brain, bank_id="b1", include_reflect=False)
        names = [t.name for t in tools]
        assert "memory_reflect" not in names


# ---------------------------------------------------------------------------
# LlamaIndex
# ---------------------------------------------------------------------------


class TestLlamaIndex:
    async def test_put_and_get(self):
        brain, _ = _make_brain()
        memory = AstrocyteLlamaMemory(brain, bank_id="b1")

        mem_id = await memory.put("Calvin prefers dark mode")
        assert mem_id is not None

        result = await memory.get("dark mode")
        assert "dark mode" in result

    async def test_get_empty(self):
        brain, _ = _make_brain()
        memory = AstrocyteLlamaMemory(brain, bank_id="empty")
        result = await memory.get("anything")
        assert result == ""

    async def test_get_all(self):
        brain, _ = _make_brain()
        memory = AstrocyteLlamaMemory(brain, bank_id="b1")

        await memory.put("Memory one")
        await memory.put("Memory two")

        all_mems = await memory.get_all()
        assert len(all_mems) >= 2

    async def test_search_with_tags(self):
        brain, _ = _make_brain()
        memory = AstrocyteLlamaMemory(brain, bank_id="b1")

        await memory.put("Tagged content", tags=["important"])
        results = await memory.search("Tagged")
        assert len(results) >= 1

    async def test_reset(self):
        brain, engine = _make_brain()
        memory = AstrocyteLlamaMemory(brain, bank_id="b1")

        await memory.put("to delete")
        assert len(engine._memories.get("b1", [])) >= 1

        await memory.reset()
        assert len(engine._memories.get("b1", [])) == 0

    async def test_metadata_source(self):
        brain, engine = _make_brain()
        memory = AstrocyteLlamaMemory(brain, bank_id="b1")

        await memory.put("test")
        mems = engine._memories["b1"]
        assert mems[0].metadata.get("source") == "llamaindex"


# ---------------------------------------------------------------------------
# Strands Agents (AWS)
# ---------------------------------------------------------------------------


class TestStrandsAgents:
    async def test_tools_created(self):
        brain, _ = _make_brain()
        tools = astrocyte_strands_tools(brain, bank_id="b1")
        names = [t["spec"]["name"] for t in tools]
        assert "memory_retain" in names
        assert "memory_recall" in names
        assert "memory_reflect" in names

    async def test_tool_has_spec_and_handler(self):
        brain, _ = _make_brain()
        tools = astrocyte_strands_tools(brain, bank_id="b1")

        for tool in tools:
            assert "spec" in tool
            assert "handler" in tool
            assert "name" in tool["spec"]
            assert "inputSchema" in tool["spec"]

    async def test_retain_handler(self):
        brain, engine = _make_brain()
        tools = astrocyte_strands_tools(brain, bank_id="b1")
        retain_tool = next(t for t in tools if t["spec"]["name"] == "memory_retain")

        result_json = await retain_tool["handler"]({"content": "Strands test memory"})
        result = json.loads(result_json)
        assert result["stored"] is True

    async def test_recall_handler(self):
        brain, _ = _make_brain()
        tools = astrocyte_strands_tools(brain, bank_id="b1")
        retain = next(t for t in tools if t["spec"]["name"] == "memory_retain")
        recall = next(t for t in tools if t["spec"]["name"] == "memory_recall")

        await retain["handler"]({"content": "AWS Lambda for serverless"})
        result_json = await recall["handler"]({"query": "Lambda"})
        result = json.loads(result_json)
        assert len(result["hits"]) >= 1

    async def test_no_reflect_when_disabled(self):
        brain, _ = _make_brain()
        tools = astrocyte_strands_tools(brain, bank_id="b1", include_reflect=False)
        names = [t["spec"]["name"] for t in tools]
        assert "memory_reflect" not in names
