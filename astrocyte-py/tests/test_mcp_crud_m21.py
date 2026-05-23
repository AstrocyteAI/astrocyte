"""Tests for M21 CRUD MCP tools (mental models + observations + directive).

Locks in: all 8 tools are registered, callable end-to-end, return
shape-stable JSON, and route through brain.<method> correctly. Uses
the in-memory engine + in-memory mental-model store so no real
storage backend is required.
"""

from __future__ import annotations

import json

import pytest

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.mcp import create_mcp_server
from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.testing.in_memory import (
    InMemoryMentalModelStore,
    InMemoryVectorStore,
    MockLLMProvider,
)


def _make_brain() -> Astrocyte:
    config = AstrocyteConfig()
    config.provider_tier = "storage"
    config.barriers.pii.mode = "disabled"
    config.escalation.degraded_mode = "error"
    config.mcp.default_bank_id = "default"
    brain = Astrocyte(config)
    pipeline = PipelineOrchestrator(
        vector_store=InMemoryVectorStore(),
        llm_provider=MockLLMProvider(),
    )
    brain.set_pipeline(pipeline)
    brain.set_mental_model_store(InMemoryMentalModelStore())
    return brain


@pytest.fixture
def mcp_brain():
    brain = _make_brain()
    config = AstrocyteConfig()
    config.mcp.default_bank_id = "default"
    config.mcp.max_results_limit = 100
    return create_mcp_server(brain, config), brain


async def _call(mcp, tool: str, args: dict) -> dict:
    result = await mcp.call_tool(tool, args)
    return json.loads(result.content[0].text)


class TestToolsRegistered:
    async def test_all_8_m21_tools_registered(self, mcp_brain):
        mcp, _ = mcp_brain
        tools = await mcp.list_tools()
        names = {t.name for t in tools}
        for expected in [
            "memory_list_mental_models",
            "memory_create_mental_model",
            "memory_update_mental_model",
            "memory_delete_mental_model",
            "memory_create_directive",
            "memory_list_observations",
            "memory_get_observation",
            "memory_create_observation",
            "memory_delete_observation",
        ]:
            assert expected in names, f"missing tool: {expected}"


class TestMentalModelCrud:
    async def test_create_with_content_then_list(self, mcp_brain):
        mcp, _ = mcp_brain
        r = await _call(
            mcp,
            "memory_create_mental_model",
            {
                "model_id": "alice",
                "title": "Alice prefs",
                "content": "## Intro\n\nAlice likes async.\n\n## Tools\n\n- Slack",
            },
        )
        assert r["model_id"] == "alice"
        assert r["revision"] == 1

        lst = await _call(mcp, "memory_list_mental_models", {})
        assert any(m["model_id"] == "alice" for m in lst["models"])
        alice = next(m for m in lst["models"] if m["model_id"] == "alice")
        assert alice["title"] == "Alice prefs"
        assert alice["kind"] == "general"

    async def test_create_with_sections_populates_structured_doc(self, mcp_brain):
        mcp, brain = mcp_brain
        r = await _call(
            mcp,
            "memory_create_mental_model",
            {
                "model_id": "bob",
                "title": "Bob status",
                "sections": [
                    {
                        "id": "status",
                        "heading": "Status",
                        "level": 2,
                        "blocks": [{"type": "paragraph", "text": "On vacation."}],
                    }
                ],
            },
        )
        assert r["model_id"] == "bob"
        model = await brain.get_mental_model("bob", bank_id="default")
        assert model is not None
        assert model.structured_doc is not None
        assert model.structured_doc["sections"][0]["id"] == "status"

    async def test_update_via_ops(self, mcp_brain):
        mcp, _ = mcp_brain
        await _call(
            mcp,
            "memory_create_mental_model",
            {
                "model_id": "x",
                "title": "X",
                "sections": [
                    {
                        "id": "main",
                        "heading": "Main",
                        "level": 2,
                        "blocks": [{"type": "paragraph", "text": "initial"}],
                    }
                ],
            },
        )
        r = await _call(
            mcp,
            "memory_update_mental_model",
            {
                "model_id": "x",
                "operations": [
                    {
                        "op": "append_block",
                        "section_id": "main",
                        "block": {"type": "paragraph", "text": "added"},
                    }
                ],
            },
        )
        assert r["changed"] is True
        assert r["revision"] == 2
        assert len(r["applied"]) == 1

    async def test_update_missing_model_returns_error(self, mcp_brain):
        mcp, _ = mcp_brain
        r = await _call(
            mcp,
            "memory_update_mental_model",
            {"model_id": "ghost", "operations": []},
        )
        assert r["changed"] is False
        assert "error" in r

    async def test_delete_returns_existed(self, mcp_brain):
        mcp, _ = mcp_brain
        await _call(
            mcp,
            "memory_create_mental_model",
            {"model_id": "to-del", "title": "T", "content": "## X\n\nbody"},
        )
        r = await _call(mcp, "memory_delete_mental_model", {"model_id": "to-del"})
        assert r["deleted"] is True
        r2 = await _call(mcp, "memory_delete_mental_model", {"model_id": "to-del"})
        assert r2["deleted"] is False


class TestDirective:
    async def test_create_directive_kind_set(self, mcp_brain):
        mcp, brain = mcp_brain
        r = await _call(
            mcp,
            "memory_create_directive",
            {"rule_text": "Always confirm before sending money"},
        )
        assert r["model_id"] is not None
        assert r["kind"] == "directive"
        model = await brain.get_mental_model(r["model_id"], bank_id="default")
        assert model is not None
        assert model.kind == "directive"
        # Structured doc populated with a single Rule section
        assert model.structured_doc is not None
        assert model.structured_doc["sections"][0]["heading"] == "Rule"

    async def test_create_directive_with_custom_id(self, mcp_brain):
        mcp, _ = mcp_brain
        r = await _call(
            mcp,
            "memory_create_directive",
            {"rule_text": "Use Sony brand", "directive_id": "directive:sony"},
        )
        assert r["model_id"] == "directive:sony"

    async def test_create_directive_empty_rule_rejected(self, mcp_brain):
        mcp, _ = mcp_brain
        r = await _call(mcp, "memory_create_directive", {"rule_text": "   "})
        # ValueError class name returned per error contract.
        assert r["model_id"] is None
        assert "error" in r


class TestObservationCrud:
    async def test_create_then_list_then_get_then_delete(self, mcp_brain):
        mcp, _ = mcp_brain
        r = await _call(
            mcp,
            "memory_create_observation",
            {"text": "User prefers async stand-ups", "confidence": 0.95},
        )
        assert r["observation"] is not None
        obs_id = r["observation"]["id"]
        assert r["observation"]["trend"] == "new"  # just-created

        # list
        lst = await _call(mcp, "memory_list_observations", {})
        assert any(o["id"] == obs_id for o in lst["observations"])

        # get
        got = await _call(mcp, "memory_get_observation", {"observation_id": obs_id})
        assert got["observation"]["text"] == "User prefers async stand-ups"

        # delete
        dr = await _call(mcp, "memory_delete_observation", {"observation_id": obs_id})
        assert dr["deleted"] is True

        # confirm gone
        got2 = await _call(mcp, "memory_get_observation", {"observation_id": obs_id})
        assert got2["observation"] is None

    async def test_list_filter_by_trend(self, mcp_brain):
        mcp, _ = mcp_brain
        await _call(
            mcp,
            "memory_create_observation",
            {"text": "Recent observation"},
        )
        # Only NEW results when filtering for new (just-created → NEW)
        lst_new = await _call(mcp, "memory_list_observations", {"trend": "new"})
        assert len(lst_new["observations"]) >= 1
        # No stale results yet
        lst_stale = await _call(mcp, "memory_list_observations", {"trend": "stale"})
        assert len(lst_stale["observations"]) == 0

    async def test_empty_text_rejected(self, mcp_brain):
        mcp, _ = mcp_brain
        r = await _call(mcp, "memory_create_observation", {"text": ""})
        # ValueError surfaces in error field
        assert r["observation"] is None
        assert "error" in r
