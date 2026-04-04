"""Tests for wave 2 integrations — Semantic Kernel, DSPy, CAMEL-AI, BeeAI, MS Agent, LiveKit, Haystack."""

import json

from astrocytes._astrocyte import Astrocyte
from astrocytes.config import AstrocyteConfig
from astrocytes.integrations.beeai import AstrocyteBeeTool, astrocyte_bee_tools
from astrocytes.integrations.camel_ai import AstrocyteCamelMemory
from astrocytes.integrations.dspy import AstrocyteRM
from astrocytes.integrations.haystack import AstrocyteDocument, AstrocyteRetriever, AstrocyteWriter
from astrocytes.integrations.livekit import AstrocyteLiveKitMemory
from astrocytes.integrations.microsoft_agent import astrocyte_ms_agent_tools
from astrocytes.integrations.semantic_kernel import AstrocytePlugin
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
# Semantic Kernel
# ---------------------------------------------------------------------------


class TestSemanticKernel:
    async def test_retain_and_recall(self):
        brain, _ = _make_brain()
        plugin = AstrocytePlugin(brain, bank_id="b1")

        result = await plugin.retain("Calvin likes dark mode")
        assert "Stored" in result

        result = await plugin.recall("dark mode")
        assert "dark mode" in result.lower()

    async def test_reflect(self):
        brain, _ = _make_brain()
        plugin = AstrocytePlugin(brain, bank_id="b1")
        await plugin.retain("Calvin prefers Python")
        answer = await plugin.reflect("What does Calvin prefer?")
        assert len(answer) > 0

    async def test_get_functions(self):
        brain, _ = _make_brain()
        plugin = AstrocytePlugin(brain, bank_id="b1")
        fns = plugin.get_functions()
        names = [f["name"] for f in fns]
        assert "retain" in names
        assert "recall" in names
        assert "reflect" in names

    async def test_no_reflect_when_disabled(self):
        brain, _ = _make_brain()
        plugin = AstrocytePlugin(brain, bank_id="b1", include_reflect=False)
        fns = plugin.get_functions()
        names = [f["name"] for f in fns]
        assert "reflect" not in names

    async def test_recall_empty(self):
        brain, _ = _make_brain()
        plugin = AstrocytePlugin(brain, bank_id="empty")
        result = await plugin.recall("anything")
        assert "No relevant" in result


# ---------------------------------------------------------------------------
# DSPy
# ---------------------------------------------------------------------------


class TestDSPy:
    async def test_aretrieve(self):
        brain, _ = _make_brain()
        rm = AstrocyteRM(brain, bank_id="b1")

        await rm.aretain("Python is great for AI")
        results = await rm.aretrieve("Python")
        assert len(results) >= 1
        assert any("Python" in r for r in results)

    async def test_aretrieve_empty(self):
        brain, _ = _make_brain()
        rm = AstrocyteRM(brain, bank_id="empty")
        results = await rm.aretrieve("anything")
        assert results == []

    async def test_aretain(self):
        brain, engine = _make_brain()
        rm = AstrocyteRM(brain, bank_id="b1")
        mem_id = await rm.aretain("DSPy test content")
        assert mem_id is not None
        assert len(engine._memories.get("b1", [])) >= 1

    async def test_areflect(self):
        brain, _ = _make_brain()
        rm = AstrocyteRM(brain, bank_id="b1")
        await rm.aretain("Calvin likes dark mode and Python")
        answer = await rm.areflect("What does Calvin like?")
        assert len(answer) > 0

    async def test_default_k(self):
        brain, _ = _make_brain()
        rm = AstrocyteRM(brain, bank_id="b1", default_k=3)
        assert rm.default_k == 3


# ---------------------------------------------------------------------------
# CAMEL-AI
# ---------------------------------------------------------------------------


class TestCamelAI:
    async def test_write_and_read(self):
        brain, _ = _make_brain()
        memory = AstrocyteCamelMemory(brain, bank_id="sim")

        mem_id = await memory.write("Patient reports headaches", role="doctor")
        assert mem_id is not None

        results = await memory.read("headaches", role="doctor")
        assert len(results) >= 1

    async def test_role_banks(self):
        brain, engine = _make_brain()
        memory = AstrocyteCamelMemory(
            brain,
            bank_id="shared",
            role_banks={"doctor": "doctor-bank", "patient": "patient-bank"},
        )

        await memory.write("Doctor note", role="doctor")
        await memory.write("Patient note", role="patient")

        assert "doctor-bank" in engine._memories
        assert "patient-bank" in engine._memories

    async def test_get_context(self):
        brain, _ = _make_brain()
        memory = AstrocyteCamelMemory(brain, bank_id="b1")
        await memory.write("Important finding")
        context = await memory.get_context("finding")
        assert "Important finding" in context

    async def test_reflect(self):
        brain, _ = _make_brain()
        memory = AstrocyteCamelMemory(brain, bank_id="b1")
        await memory.write("Calvin likes Python and dark mode")
        answer = await memory.reflect("What does Calvin like?")
        assert len(answer) > 0

    async def test_clear(self):
        brain, engine = _make_brain()
        memory = AstrocyteCamelMemory(brain, bank_id="b1")
        await memory.write("to clear")
        assert len(engine._memories.get("b1", [])) >= 1
        await memory.clear()
        assert len(engine._memories.get("b1", [])) == 0

    async def test_tags_include_role(self):
        brain, engine = _make_brain()
        memory = AstrocyteCamelMemory(brain, bank_id="b1")
        await memory.write("tagged", role="analyst")
        mems = engine._memories["b1"]
        assert "role:analyst" in (mems[0].tags or [])


# ---------------------------------------------------------------------------
# BeeAI
# ---------------------------------------------------------------------------


class TestBeeAI:
    async def test_tools_created(self):
        brain, _ = _make_brain()
        tools = astrocyte_bee_tools(brain, bank_id="b1")
        names = [t.name for t in tools]
        assert "memory_retain" in names
        assert "memory_recall" in names
        assert "memory_reflect" in names

    async def test_tool_is_bee_tool(self):
        brain, _ = _make_brain()
        tools = astrocyte_bee_tools(brain, bank_id="b1")
        assert all(isinstance(t, AstrocyteBeeTool) for t in tools)

    async def test_retain_via_run(self):
        brain, engine = _make_brain()
        tools = astrocyte_bee_tools(brain, bank_id="b1")
        retain = next(t for t in tools if t.name == "memory_retain")

        result_json = await retain.run({"content": "BeeAI test memory"})
        result = json.loads(result_json)
        assert result["stored"] is True

    async def test_recall_via_run(self):
        brain, _ = _make_brain()
        tools = astrocyte_bee_tools(brain, bank_id="b1")
        retain = next(t for t in tools if t.name == "memory_retain")
        recall = next(t for t in tools if t.name == "memory_recall")

        await retain.run({"content": "Python for enterprise AI"})
        result_json = await recall.run({"query": "Python"})
        result = json.loads(result_json)
        assert len(result["hits"]) >= 1

    async def test_no_reflect_when_disabled(self):
        brain, _ = _make_brain()
        tools = astrocyte_bee_tools(brain, bank_id="b1", include_reflect=False)
        names = [t.name for t in tools]
        assert "memory_reflect" not in names


# ---------------------------------------------------------------------------
# Microsoft Agent Framework
# ---------------------------------------------------------------------------


class TestMicrosoftAgentFramework:
    async def test_tools_and_handlers(self):
        brain, _ = _make_brain()
        tools, handlers = astrocyte_ms_agent_tools(brain, bank_id="b1")

        assert len(tools) >= 2
        assert "memory_retain" in handlers
        assert "memory_recall" in handlers

    async def test_retain_handler(self):
        brain, engine = _make_brain()
        _, handlers = astrocyte_ms_agent_tools(brain, bank_id="b1")

        result_json = await handlers["memory_retain"](content="MS Agent test")
        result = json.loads(result_json)
        assert result["stored"] is True

    async def test_recall_handler(self):
        brain, _ = _make_brain()
        _, handlers = astrocyte_ms_agent_tools(brain, bank_id="b1")

        await handlers["memory_retain"](content="Dark mode preference")
        result_json = await handlers["memory_recall"](query="dark mode")
        result = json.loads(result_json)
        assert len(result["hits"]) >= 1


# ---------------------------------------------------------------------------
# LiveKit Agents
# ---------------------------------------------------------------------------


class TestLiveKitAgents:
    async def test_retain_and_context(self):
        brain, _ = _make_brain()
        memory = AstrocyteLiveKitMemory(brain, bank_id="session")

        await memory.retain_from_session("User prefers morning appointments")
        context = await memory.get_session_context("appointments")
        assert "morning" in context

    async def test_mid_session_recall(self):
        brain, _ = _make_brain()
        memory = AstrocyteLiveKitMemory(brain, bank_id="session")

        await memory.retain_from_session("User's name is Calvin")
        results = await memory.recall_mid_session("name")
        assert len(results) >= 1
        assert any("Calvin" in r["text"] for r in results)

    async def test_session_bank_prefix(self):
        brain, engine = _make_brain()
        memory = AstrocyteLiveKitMemory(
            brain,
            bank_id="default",
            session_bank_prefix="session-",
        )

        await memory.retain_from_session("Session content", session_id="abc123")
        assert "session-abc123" in engine._memories

    async def test_summarize_session(self):
        brain, _ = _make_brain()
        memory = AstrocyteLiveKitMemory(brain, bank_id="b1")
        await memory.retain_from_session("User asked about pricing")
        await memory.retain_from_session("User mentioned budget constraints")
        summary = await memory.summarize_session()
        assert len(summary) > 0

    async def test_empty_context(self):
        brain, _ = _make_brain()
        memory = AstrocyteLiveKitMemory(brain, bank_id="empty")
        context = await memory.get_session_context("anything")
        assert context == ""

    async def test_tags_include_livekit(self):
        brain, engine = _make_brain()
        memory = AstrocyteLiveKitMemory(brain, bank_id="b1")
        await memory.retain_from_session("tagged content")
        mems = engine._memories["b1"]
        assert "livekit" in (mems[0].tags or [])


# ---------------------------------------------------------------------------
# Haystack
# ---------------------------------------------------------------------------


class TestHaystack:
    async def test_retriever_arun(self):
        brain, _ = _make_brain()
        retriever = AstrocyteRetriever(brain, bank_id="b1")

        await brain.retain("Haystack retrieval test content", bank_id="b1")
        result = await retriever.arun("Haystack")
        assert "documents" in result
        assert len(result["documents"]) >= 1
        assert isinstance(result["documents"][0], AstrocyteDocument)

    async def test_document_fields(self):
        brain, _ = _make_brain()
        retriever = AstrocyteRetriever(brain, bank_id="b1")
        await brain.retain("Document test", bank_id="b1")

        result = await retriever.arun("Document")
        doc = result["documents"][0]
        assert doc.content
        assert doc.score > 0
        assert isinstance(doc.meta, dict)

    async def test_retriever_empty(self):
        brain, _ = _make_brain()
        retriever = AstrocyteRetriever(brain, bank_id="empty")
        result = await retriever.arun("anything")
        assert result["documents"] == []

    async def test_writer_arun(self):
        brain, engine = _make_brain()
        writer = AstrocyteWriter(brain, bank_id="b1")

        docs = [
            AstrocyteDocument(content="Doc one", meta={"source": "test"}, score=1.0, id="d1"),
            AstrocyteDocument(content="Doc two", meta={"source": "test"}, score=1.0, id="d2"),
        ]
        result = await writer.arun(docs)
        assert result["written"] == 2
        assert len(engine._memories.get("b1", [])) >= 2

    async def test_writer_with_dicts(self):
        brain, engine = _make_brain()
        writer = AstrocyteWriter(brain, bank_id="b1")

        docs = [
            {"content": "Dict doc", "meta": {"source": "test"}},
        ]
        result = await writer.arun(docs)
        assert result["written"] == 1

    async def test_top_k(self):
        brain, _ = _make_brain()
        retriever = AstrocyteRetriever(brain, bank_id="b1", top_k=2)
        for i in range(5):
            await brain.retain(f"Memory about testing item {i}", bank_id="b1")
        result = await retriever.arun("testing")
        assert len(result["documents"]) <= 2
