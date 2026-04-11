"""Tests for Managed Agents integration helpers.

Tests bank naming conventions and the extracted handler logic
(coordinator retain/recall, sub-agent recall, cross-bank queries).
No claude_agent_sdk dependency required.
"""

import json

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.integrations.managed_agents import (
    _handle_coordinator_recall,
    _handle_coordinator_reflect,
    _handle_retain,
    _handle_subagent_recall,
    _parse_tags,
    agent_bank_id,
    coordinator_bank_id,
    session_bank_id,
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
# Bank naming
# ---------------------------------------------------------------------------


class TestBankNaming:
    def test_session_bank_id(self):
        assert session_bank_id("abc-123") == "session-abc-123"

    def test_agent_bank_id(self):
        assert agent_bank_id("abc-123", "researcher") == "session-abc-123:agent-researcher"

    def test_coordinator_bank_id(self):
        assert coordinator_bank_id("abc-123") == "session-abc-123:coordinator"

    def test_banks_are_distinct(self):
        sid = "sess-001"
        banks = {
            session_bank_id(sid),
            agent_bank_id(sid, "researcher"),
            agent_bank_id(sid, "analyst"),
            coordinator_bank_id(sid),
        }
        assert len(banks) == 4, "All bank IDs must be distinct"

    def test_agent_banks_isolated_by_role(self):
        sid = "same-session"
        assert agent_bank_id(sid, "a") != agent_bank_id(sid, "b")

    def test_agent_banks_isolated_by_session(self):
        assert agent_bank_id("s1", "role") != agent_bank_id("s2", "role")


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

    def test_none(self):
        assert _parse_tags(None) is None

    def test_non_string(self):
        assert _parse_tags(42) is None


# ---------------------------------------------------------------------------
# Coordinator handlers
# ---------------------------------------------------------------------------


class TestCoordinatorRetain:
    async def test_stores_to_coordinator_bank(self):
        brain, engine = _make_brain()
        coord_bank = coordinator_bank_id("s1")

        result = await _handle_retain(brain, coord_bank, {"content": "shared finding"})

        data = json.loads(result["content"][0]["text"])
        assert data["stored"] is True
        assert data["memory_id"] is not None
        # Verify it's in the coordinator bank
        mems = engine._memories.get(coord_bank, [])
        assert len(mems) == 1
        assert "shared finding" in mems[0].text

    async def test_stores_with_tags(self):
        brain, engine = _make_brain()
        coord_bank = coordinator_bank_id("s1")

        await _handle_retain(brain, coord_bank, {"content": "tagged", "tags": "decision,important"})

        mems = engine._memories.get(coord_bank, [])
        assert mems[0].tags == ["decision", "important"]


class TestCoordinatorRecall:
    async def test_recall_from_coordinator_bank_only(self):
        brain, _ = _make_brain()
        sid = "s1"
        coord_bank = coordinator_bank_id(sid)

        await brain.retain("coordinator note about Python", bank_id=coord_bank)

        result = await _handle_coordinator_recall(brain, sid, coord_bank, {"query": "Python"})

        data = json.loads(result["content"][0]["text"])
        assert len(data["hits"]) >= 1
        assert data["hits"][0]["bank_id"] == coord_bank

    async def test_recall_includes_subagent_banks(self):
        brain, _ = _make_brain()
        sid = "s1"
        coord_bank = coordinator_bank_id(sid)
        researcher_bank = agent_bank_id(sid, "researcher")

        await brain.retain("coord insight", bank_id=coord_bank)
        await brain.retain("researcher finding about APIs", bank_id=researcher_bank)

        result = await _handle_coordinator_recall(
            brain,
            sid,
            coord_bank,
            {"query": "APIs", "include_agents": "researcher"},
        )

        data = json.loads(result["content"][0]["text"])
        bank_ids = {h["bank_id"] for h in data["hits"]}
        assert researcher_bank in bank_ids

    async def test_recall_empty_include_agents(self):
        brain, _ = _make_brain()
        sid = "s1"
        coord_bank = coordinator_bank_id(sid)

        await brain.retain("only coordinator", bank_id=coord_bank)

        result = await _handle_coordinator_recall(
            brain,
            sid,
            coord_bank,
            {"query": "coordinator", "include_agents": ""},
        )

        data = json.loads(result["content"][0]["text"])
        assert len(data["hits"]) >= 1

    async def test_recall_multiple_agents(self):
        brain, _ = _make_brain()
        sid = "s1"
        coord_bank = coordinator_bank_id(sid)
        r_bank = agent_bank_id(sid, "researcher")
        a_bank = agent_bank_id(sid, "analyst")

        await brain.retain("research data", bank_id=r_bank)
        await brain.retain("analysis result", bank_id=a_bank)

        result = await _handle_coordinator_recall(
            brain,
            sid,
            coord_bank,
            {"query": "data result", "include_agents": "researcher, analyst"},
        )

        data = json.loads(result["content"][0]["text"])
        bank_ids = {h["bank_id"] for h in data["hits"]}
        assert r_bank in bank_ids or a_bank in bank_ids


class TestCoordinatorReflect:
    async def test_reflect_from_coordinator(self):
        brain, _ = _make_brain()
        sid = "s1"
        coord_bank = coordinator_bank_id(sid)

        await brain.retain("Calvin prefers dark mode", bank_id=coord_bank)

        result = await _handle_coordinator_reflect(brain, sid, coord_bank, {"query": "What does Calvin prefer?"})

        # Reflect returns the answer as plain text
        answer = result["content"][0]["text"]
        assert len(answer) > 0

    async def test_reflect_includes_subagent_banks(self):
        brain, _ = _make_brain()
        sid = "s1"
        coord_bank = coordinator_bank_id(sid)
        r_bank = agent_bank_id(sid, "researcher")

        await brain.retain("Python is the primary language", bank_id=r_bank)

        result = await _handle_coordinator_reflect(
            brain,
            sid,
            coord_bank,
            {"query": "What language?", "include_agents": "researcher"},
        )

        answer = result["content"][0]["text"]
        assert len(answer) > 0


# ---------------------------------------------------------------------------
# Sub-agent handlers
# ---------------------------------------------------------------------------


class TestSubagentRetain:
    async def test_stores_to_own_bank(self):
        brain, engine = _make_brain()
        own_bank = agent_bank_id("s1", "researcher")

        result = await _handle_retain(brain, own_bank, {"content": "private finding"})

        data = json.loads(result["content"][0]["text"])
        assert data["stored"] is True
        mems = engine._memories.get(own_bank, [])
        assert len(mems) == 1

    async def test_does_not_write_to_coordinator(self):
        brain, engine = _make_brain()
        own_bank = agent_bank_id("s1", "researcher")
        coord_bank = coordinator_bank_id("s1")

        await _handle_retain(brain, own_bank, {"content": "private"})

        assert len(engine._memories.get(coord_bank, [])) == 0


class TestSubagentRecall:
    async def test_reads_own_bank(self):
        brain, _ = _make_brain()
        own_bank = agent_bank_id("s1", "researcher")
        coord_bank = coordinator_bank_id("s1")

        await brain.retain("my own finding", bank_id=own_bank)

        result = await _handle_subagent_recall(brain, own_bank, coord_bank, {"query": "finding"})

        data = json.loads(result["content"][0]["text"])
        assert len(data["hits"]) >= 1
        assert data["hits"][0]["bank_id"] == own_bank

    async def test_reads_coordinator_bank(self):
        brain, _ = _make_brain()
        own_bank = agent_bank_id("s1", "researcher")
        coord_bank = coordinator_bank_id("s1")

        await brain.retain("shared directive from coordinator", bank_id=coord_bank)

        result = await _handle_subagent_recall(brain, own_bank, coord_bank, {"query": "directive"})

        data = json.loads(result["content"][0]["text"])
        assert len(data["hits"]) >= 1
        assert data["hits"][0]["bank_id"] == coord_bank

    async def test_does_not_read_other_agent_bank(self):
        brain, _ = _make_brain()
        own_bank = agent_bank_id("s1", "researcher")
        coord_bank = coordinator_bank_id("s1")
        other_bank = agent_bank_id("s1", "analyst")

        await brain.retain("analyst secret", bank_id=other_bank)

        result = await _handle_subagent_recall(brain, own_bank, coord_bank, {"query": "analyst secret"})

        data = json.loads(result["content"][0]["text"])
        bank_ids = {h["bank_id"] for h in data["hits"]}
        assert other_bank not in bank_ids


# ---------------------------------------------------------------------------
# Cross-agent isolation
# ---------------------------------------------------------------------------


class TestCrossAgentIsolation:
    async def test_agents_cannot_see_each_others_banks(self):
        """Two sub-agents in the same session cannot read each other's banks."""
        brain, _ = _make_brain()
        sid = "s1"
        coord_bank = coordinator_bank_id(sid)
        r_bank = agent_bank_id(sid, "researcher")
        a_bank = agent_bank_id(sid, "analyst")

        await brain.retain("researcher secret", bank_id=r_bank)
        await brain.retain("analyst secret", bank_id=a_bank)

        # Researcher can't see analyst
        r_result = await _handle_subagent_recall(brain, r_bank, coord_bank, {"query": "analyst secret"})
        r_data = json.loads(r_result["content"][0]["text"])
        r_banks = {h["bank_id"] for h in r_data["hits"]}
        assert a_bank not in r_banks

        # Analyst can't see researcher
        a_result = await _handle_subagent_recall(brain, a_bank, coord_bank, {"query": "researcher secret"})
        a_data = json.loads(a_result["content"][0]["text"])
        a_banks = {h["bank_id"] for h in a_data["hits"]}
        assert r_bank not in a_banks

    async def test_coordinator_can_see_all_banks(self):
        """Coordinator can query across all sub-agent banks."""
        brain, _ = _make_brain()
        sid = "s1"
        coord_bank = coordinator_bank_id(sid)
        r_bank = agent_bank_id(sid, "researcher")
        a_bank = agent_bank_id(sid, "analyst")

        await brain.retain("researcher data point", bank_id=r_bank)
        await brain.retain("analyst conclusion", bank_id=a_bank)

        result = await _handle_coordinator_recall(
            brain,
            sid,
            coord_bank,
            {"query": "data conclusion", "include_agents": "researcher,analyst"},
        )

        data = json.loads(result["content"][0]["text"])
        bank_ids = {h["bank_id"] for h in data["hits"]}
        assert r_bank in bank_ids or a_bank in bank_ids

    async def test_different_sessions_fully_isolated(self):
        """Agents in different sessions cannot see each other's memory."""
        brain, _ = _make_brain()

        bank_s1 = agent_bank_id("s1", "researcher")
        bank_s2 = agent_bank_id("s2", "researcher")
        coord_s2 = coordinator_bank_id("s2")

        await brain.retain("session 1 secret", bank_id=bank_s1)

        result = await _handle_subagent_recall(brain, bank_s2, coord_s2, {"query": "session 1 secret"})

        data = json.loads(result["content"][0]["text"])
        bank_ids = {h["bank_id"] for h in data["hits"]}
        assert bank_s1 not in bank_ids
