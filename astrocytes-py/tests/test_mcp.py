"""Tests for astrocytes.mcp — MCP server tool wiring and behavior."""

import json
from pathlib import Path

from astrocytes._astrocyte import Astrocyte
from astrocytes.config import AstrocyteConfig, load_config
from astrocytes.mcp import create_mcp_server
from astrocytes.testing.in_memory import InMemoryEngineProvider


def _make_mcp(
    expose_reflect: bool = True,
    expose_forget: bool = True,
    default_bank: str | None = "default",
):
    """Create an MCP server backed by an in-memory engine."""
    config = AstrocyteConfig()
    config.provider = "test"
    config.barriers.pii.mode = "disabled"
    config.mcp.default_bank_id = default_bank
    config.mcp.expose_reflect = expose_reflect
    config.mcp.expose_forget = expose_forget
    config.mcp.max_results_limit = 20

    brain = Astrocyte(config)
    engine = InMemoryEngineProvider()
    brain.set_engine_provider(engine)

    mcp = create_mcp_server(brain, config)
    return mcp, brain, engine


async def _tool_names(mcp) -> set[str]:
    tools = await mcp.list_tools()
    return {t.name for t in tools}


async def _call(mcp, tool_name: str, args: dict) -> dict:
    """Call an MCP tool and parse the JSON result."""
    result = await mcp.call_tool(tool_name, args)
    # FastMCP 3.x returns CallToolResult with content list
    text = result.content[0].text
    return json.loads(text)


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


class TestToolRegistration:
    async def test_core_tools_registered(self):
        mcp, _, _ = _make_mcp()
        names = await _tool_names(mcp)
        assert "memory_retain" in names
        assert "memory_recall" in names
        assert "memory_banks" in names
        assert "memory_health" in names

    async def test_reflect_exposed_by_default(self):
        mcp, _, _ = _make_mcp(expose_reflect=True)
        names = await _tool_names(mcp)
        assert "memory_reflect" in names

    async def test_reflect_hidden_when_disabled(self):
        mcp, _, _ = _make_mcp(expose_reflect=False)
        names = await _tool_names(mcp)
        assert "memory_reflect" not in names

    async def test_forget_exposed_when_enabled(self):
        mcp, _, _ = _make_mcp(expose_forget=True)
        names = await _tool_names(mcp)
        assert "memory_forget" in names

    async def test_forget_hidden_when_disabled(self):
        mcp, _, _ = _make_mcp(expose_forget=False)
        names = await _tool_names(mcp)
        assert "memory_forget" not in names


# ---------------------------------------------------------------------------
# Tool execution via call_tool
# ---------------------------------------------------------------------------


class TestMemoryRetain:
    async def test_retain_basic(self):
        mcp, _, _ = _make_mcp()
        result = await _call(mcp, "memory_retain", {"content": "Calvin likes dark mode"})
        assert result["stored"] is True
        assert result["memory_id"] is not None

    async def test_retain_with_bank(self):
        mcp, _, _ = _make_mcp()
        result = await _call(mcp, "memory_retain", {"content": "Test", "bank_id": "custom-bank"})
        assert result["stored"] is True

    async def test_retain_with_tags(self):
        mcp, _, _ = _make_mcp()
        result = await _call(mcp, "memory_retain", {"content": "Tagged", "tags": ["pref", "ui"]})
        assert result["stored"] is True


class TestMemoryRecall:
    async def test_recall_basic(self):
        mcp, _, _ = _make_mcp()
        await _call(mcp, "memory_retain", {"content": "Calvin prefers dark mode"})
        result = await _call(mcp, "memory_recall", {"query": "dark mode"})
        assert len(result["hits"]) >= 1
        assert "dark mode" in result["hits"][0]["text"].lower()

    async def test_recall_max_results_capped(self):
        mcp, _, _ = _make_mcp()
        for i in range(25):
            await _call(mcp, "memory_retain", {"content": f"Memory about testing item {i}"})
        result = await _call(mcp, "memory_recall", {"query": "testing", "max_results": 100})
        assert len(result["hits"]) <= 20  # Capped by max_results_limit

    async def test_recall_multi_bank(self):
        mcp, _, _ = _make_mcp()
        await _call(mcp, "memory_retain", {"content": "Personal note", "bank_id": "personal"})
        await _call(mcp, "memory_retain", {"content": "Team note", "bank_id": "team"})
        result = await _call(
            mcp,
            "memory_recall",
            {
                "query": "note",
                "banks": ["personal", "team"],
            },
        )
        assert result["total_available"] >= 1


class TestMemoryReflect:
    async def test_reflect_basic(self):
        mcp, _, _ = _make_mcp()
        await _call(mcp, "memory_retain", {"content": "Calvin likes Python and dark mode"})
        result = await _call(mcp, "memory_reflect", {"query": "What does Calvin like?"})
        assert result["answer"]
        assert len(result["answer"]) > 0

    async def test_reflect_with_sources(self):
        mcp, _, _ = _make_mcp()
        await _call(mcp, "memory_retain", {"content": "Important fact about Python"})
        result = await _call(
            mcp,
            "memory_reflect",
            {
                "query": "Python",
                "include_sources": True,
            },
        )
        assert "sources" in result

    async def test_reflect_multi_bank(self):
        mcp, _, _ = _make_mcp()
        await _call(mcp, "memory_retain", {"content": "Personal pref", "bank_id": "p"})
        await _call(mcp, "memory_retain", {"content": "Team policy", "bank_id": "t"})
        result = await _call(
            mcp,
            "memory_reflect",
            {
                "query": "preferences and policies",
                "banks": ["p", "t"],
            },
        )
        assert result["answer"]


class TestMemoryForget:
    async def test_forget_by_id(self):
        mcp, _, _ = _make_mcp(expose_forget=True)
        retain_result = await _call(mcp, "memory_retain", {"content": "To delete"})
        mem_id = retain_result["memory_id"]

        forget_result = await _call(mcp, "memory_forget", {"memory_ids": [mem_id]})
        assert forget_result["deleted_count"] >= 1


class TestMemoryBanks:
    async def test_banks_returns_default(self):
        mcp, _, _ = _make_mcp(default_bank="my-default")
        result = await _call(mcp, "memory_banks", {})
        assert result["default"] == "my-default"
        assert "my-default" in result["banks"]


class TestMemoryHealth:
    async def test_health_ok(self):
        mcp, _, _ = _make_mcp()
        result = await _call(mcp, "memory_health", {})
        assert result["healthy"] is True


# ---------------------------------------------------------------------------
# Config-driven MCP
# ---------------------------------------------------------------------------


class TestMcpFromConfig:
    async def test_create_from_config_file(self, tmp_path: Path):
        config_path = tmp_path / "astrocytes.yaml"
        config_path.write_text("""
profile: minimal
provider_tier: engine
provider: test
mcp:
  default_bank_id: my-bank
  expose_reflect: true
  expose_forget: false
  max_results_limit: 25
""")
        config = load_config(config_path)
        brain = Astrocyte(config)
        brain.set_engine_provider(InMemoryEngineProvider())

        mcp = create_mcp_server(brain, config)
        names = await _tool_names(mcp)
        assert "memory_retain" in names
        assert "memory_reflect" in names
        assert "memory_forget" not in names  # Disabled in config
