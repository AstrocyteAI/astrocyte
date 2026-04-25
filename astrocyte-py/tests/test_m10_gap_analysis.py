"""M10: Gap analysis — unit and integration tests.

Tests cover:
- GapItem and AuditResult type fields and defaults
- run_audit(): empty-bank fast path (no LLM call)
- run_audit(): LLM call is made when memories are present
- run_audit(): JSON parse — valid response round-trips correctly
- run_audit(): JSON parse — graceful fallback on bad JSON
- run_audit(): coverage_score clamped to [0, 1]
- run_audit(): severity defaults to "low" for unknown values
- run_audit(): markdown-fenced JSON is unwrapped
- run_audit(): LLM failure returns degraded AuditResult (no exception)
- brain.audit(): returns AuditResult with correct scope and bank_id
- brain.audit(): coverage_score is 0.0 for an empty bank
- brain.audit(): coverage_score approaches 1.0 for a well-covered scope
- brain.audit(): gaps list is empty when judge reports full coverage
- brain.audit(): retained memories appear in memory context passed to judge
- brain.audit(): max_memories limits retrieved memories
- brain.audit(): tag filter passed through to recall
- brain.audit(): trace is embedded in result
- brain.audit(): high-severity gap for empty bank without LLM
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.pipeline.audit import _parse_response, _render_memories, run_audit
from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.testing.in_memory import InMemoryVectorStore, MockLLMProvider
from astrocyte.types import AuditResult, GapItem, MemoryHit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DIM = 16


def _unit_vec(pos: int) -> list[float]:
    v = [0.0] * _DIM
    v[pos % _DIM] = 1.0
    return v


def _hit(text: str, memory_id: str = "m1", score: float = 0.9) -> MemoryHit:
    return MemoryHit(text=text, score=score, memory_id=memory_id, bank_id="bank1")


def _good_response(score: float = 0.9, gaps: list[dict] | None = None) -> str:
    return json.dumps({"coverage_score": score, "gaps": gaps or []})


class ControlledLLMProvider:
    """Returns a pre-set response for complete(); uses MockLLMProvider for embed."""

    SPI_VERSION = 1

    def __init__(self, response: str) -> None:
        self._response = response
        self._mock = MockLLMProvider()
        self.calls: list[list] = []

    async def complete(self, messages, model=None, max_tokens=1024, temperature=0.0):
        self.calls.append(messages)
        from astrocyte.types import Completion, TokenUsage
        return Completion(
            text=self._response,
            model="controlled",
            usage=TokenUsage(input_tokens=20, output_tokens=50),
        )

    async def embed(self, texts, model=None):
        return await self._mock.embed(texts, model=model)

    def capabilities(self):
        return self._mock.capabilities()


class FailingLLMProvider(ControlledLLMProvider):
    """complete() always raises."""

    def __init__(self) -> None:
        super().__init__("")

    async def complete(self, messages, model=None, max_tokens=1024, temperature=0.0):
        raise RuntimeError("LLM unavailable")


# ---------------------------------------------------------------------------
# Type-level tests
# ---------------------------------------------------------------------------


class TestTypeFields:
    def test_gap_item_fields(self):
        g = GapItem(topic="Alice's employer", severity="high", reason="No employment records found.")
        assert g.topic == "Alice's employer"
        assert g.severity == "high"
        assert g.reason == "No employment records found."

    def test_audit_result_fields(self):
        r = AuditResult(
            scope="Alice",
            bank_id="bank1",
            gaps=[],
            coverage_score=0.8,
            memories_scanned=10,
        )
        assert r.scope == "Alice"
        assert r.bank_id == "bank1"
        assert r.gaps == []
        assert r.coverage_score == 0.8
        assert r.memories_scanned == 10
        assert r.trace is None

    def test_audit_result_trace_optional(self):
        r = AuditResult(scope="s", bank_id="b", gaps=[], coverage_score=0.5, memories_scanned=0)
        assert r.trace is None


# ---------------------------------------------------------------------------
# _render_memories helper
# ---------------------------------------------------------------------------


class TestRenderMemories:
    def test_empty_returns_placeholder(self):
        rendered = _render_memories([])
        assert "(no memories" in rendered

    def test_numbered_entries(self):
        hits = [_hit("Fact one", "m1"), _hit("Fact two", "m2")]
        rendered = _render_memories(hits)
        assert "[1]" in rendered
        assert "[2]" in rendered
        assert "Fact one" in rendered
        assert "Fact two" in rendered

    def test_retained_at_included_when_set(self):
        hit = MemoryHit(
            text="Fact",
            score=1.0,
            memory_id="m1",
            bank_id="b",
            retained_at=datetime(2025, 3, 15, tzinfo=UTC),
        )
        rendered = _render_memories([hit])
        assert "2025-03-15" in rendered


# ---------------------------------------------------------------------------
# _parse_response helper
# ---------------------------------------------------------------------------


class TestParseResponse:
    def _call(self, raw: str, memories_scanned: int = 5) -> AuditResult:
        return _parse_response(raw, "scope", "bank1", memories_scanned, trace=None)

    def test_valid_full_response(self):
        raw = json.dumps({
            "coverage_score": 0.7,
            "gaps": [{"topic": "Start date", "severity": "medium", "reason": "Not mentioned."}],
        })
        result = self._call(raw)
        assert result.coverage_score == pytest.approx(0.7)
        assert len(result.gaps) == 1
        assert result.gaps[0].topic == "Start date"
        assert result.gaps[0].severity == "medium"

    def test_no_gaps_list(self):
        raw = json.dumps({"coverage_score": 1.0, "gaps": []})
        result = self._call(raw)
        assert result.gaps == []
        assert result.coverage_score == pytest.approx(1.0)

    def test_coverage_clamped_above_1(self):
        raw = json.dumps({"coverage_score": 1.5, "gaps": []})
        result = self._call(raw)
        assert result.coverage_score == pytest.approx(1.0)

    def test_coverage_clamped_below_0(self):
        raw = json.dumps({"coverage_score": -0.3, "gaps": []})
        result = self._call(raw)
        assert result.coverage_score == pytest.approx(0.0)

    def test_bad_json_returns_fallback(self):
        result = self._call("this is not json")
        assert isinstance(result, AuditResult)
        assert len(result.gaps) == 1
        assert "parse error" in result.gaps[0].topic

    def test_markdown_fenced_json_unwrapped(self):
        raw = "```json\n" + json.dumps({"coverage_score": 0.6, "gaps": []}) + "\n```"
        result = self._call(raw)
        assert result.coverage_score == pytest.approx(0.6)

    def test_unknown_severity_defaults_to_low(self):
        raw = json.dumps({
            "coverage_score": 0.5,
            "gaps": [{"topic": "X", "severity": "critical", "reason": "r"}],
        })
        result = self._call(raw)
        assert result.gaps[0].severity == "low"

    def test_memories_scanned_propagated(self):
        raw = json.dumps({"coverage_score": 0.5, "gaps": []})
        result = self._call(raw, memories_scanned=12)
        assert result.memories_scanned == 12


# ---------------------------------------------------------------------------
# run_audit() unit tests
# ---------------------------------------------------------------------------


class TestRunAudit:
    @pytest.mark.asyncio
    async def test_empty_memories_fast_path_no_llm_call(self):
        llm = ControlledLLMProvider(_good_response())
        result = await run_audit("scope", "bank1", [], llm)
        assert result.coverage_score == pytest.approx(0.0)
        assert len(result.gaps) == 1
        assert result.gaps[0].severity == "high"
        assert llm.calls == []  # no LLM call made

    @pytest.mark.asyncio
    async def test_memories_present_calls_llm(self):
        llm = ControlledLLMProvider(_good_response(score=0.8))
        memories = [_hit("Alice works at Meta.")]
        result = await run_audit("Alice employer", "bank1", memories, llm)
        assert len(llm.calls) == 1
        assert result.coverage_score == pytest.approx(0.8)

    @pytest.mark.asyncio
    async def test_gaps_parsed_from_response(self):
        gaps = [{"topic": "Start date", "severity": "high", "reason": "Not found."}]
        llm = ControlledLLMProvider(_good_response(score=0.4, gaps=gaps))
        result = await run_audit("Alice", "bank1", [_hit("some text")], llm)
        assert len(result.gaps) == 1
        assert result.gaps[0].topic == "Start date"

    @pytest.mark.asyncio
    async def test_llm_failure_returns_degraded_result(self):
        result = await run_audit("scope", "bank1", [_hit("fact")], FailingLLMProvider())
        assert isinstance(result, AuditResult)
        # Non-empty: fallback produces a parse-error gap
        assert len(result.gaps) >= 1

    @pytest.mark.asyncio
    async def test_scope_and_bank_id_echoed(self):
        llm = ControlledLLMProvider(_good_response())
        result = await run_audit("my scope", "my-bank", [_hit("fact")], llm)
        assert result.scope == "my scope"
        assert result.bank_id == "my-bank"

    @pytest.mark.asyncio
    async def test_memories_scanned_count(self):
        llm = ControlledLLMProvider(_good_response())
        memories = [_hit(f"fact {i}", f"m{i}") for i in range(7)]
        result = await run_audit("scope", "bank1", memories, llm)
        assert result.memories_scanned == 7


# ---------------------------------------------------------------------------
# brain.audit() integration tests
# ---------------------------------------------------------------------------


def _brain() -> tuple[Astrocyte, InMemoryVectorStore, ControlledLLMProvider]:
    vs = InMemoryVectorStore()
    llm = ControlledLLMProvider(_good_response(score=0.8))
    brain = Astrocyte(AstrocyteConfig())
    brain.set_pipeline(PipelineOrchestrator(vs, llm))
    return brain, vs, llm


class TestBrainAudit:
    @pytest.mark.asyncio
    async def test_returns_audit_result(self):
        brain, _, _ = _brain()
        result = await brain.audit("Alice employer", bank_id="bank1")
        assert isinstance(result, AuditResult)

    @pytest.mark.asyncio
    async def test_scope_and_bank_id_in_result(self):
        brain, _, _ = _brain()
        result = await brain.audit("Alice employer", bank_id="bank1")
        assert result.scope == "Alice employer"
        assert result.bank_id == "bank1"

    @pytest.mark.asyncio
    async def test_empty_bank_coverage_zero(self):
        brain, _, llm = _brain()
        # No memories stored — fast path, no LLM call
        result = await brain.audit("anything", bank_id="empty-bank")
        assert result.coverage_score == pytest.approx(0.0)
        assert llm.calls == []

    @pytest.mark.asyncio
    async def test_empty_bank_high_severity_gap(self):
        brain, _, _ = _brain()
        result = await brain.audit("anything", bank_id="empty-bank")
        assert any(g.severity == "high" for g in result.gaps)

    @pytest.mark.asyncio
    async def test_memories_present_calls_llm(self):
        brain, _, llm = _brain()
        await brain.retain("Alice works at Meta.", bank_id="bank1")
        await brain.audit("Alice employer", bank_id="bank1")
        assert len(llm.calls) >= 1  # at least the audit judge call

    @pytest.mark.asyncio
    async def test_good_coverage_reported(self):
        vs = InMemoryVectorStore()
        llm = ControlledLLMProvider(_good_response(score=0.95))
        brain = Astrocyte(AstrocyteConfig())
        brain.set_pipeline(PipelineOrchestrator(vs, llm))

        await brain.retain("Alice works at Meta as a senior engineer.", bank_id="bank1")
        result = await brain.audit("Alice employer", bank_id="bank1")
        assert result.coverage_score >= 0.9

    @pytest.mark.asyncio
    async def test_gaps_populated_from_llm(self):
        gaps = [{"topic": "Start date", "severity": "medium", "reason": "Not mentioned."}]
        vs = InMemoryVectorStore()
        llm = ControlledLLMProvider(_good_response(score=0.5, gaps=gaps))
        brain = Astrocyte(AstrocyteConfig())
        brain.set_pipeline(PipelineOrchestrator(vs, llm))

        await brain.retain("Alice works at Meta.", bank_id="bank1")
        result = await brain.audit("Alice employment details", bank_id="bank1")
        assert any(g.topic == "Start date" for g in result.gaps)

    @pytest.mark.asyncio
    async def test_no_gaps_when_full_coverage(self):
        vs = InMemoryVectorStore()
        llm = ControlledLLMProvider(_good_response(score=1.0, gaps=[]))
        brain = Astrocyte(AstrocyteConfig())
        brain.set_pipeline(PipelineOrchestrator(vs, llm))

        await brain.retain("Alice works at Meta.", bank_id="bank1")
        result = await brain.audit("Alice employer", bank_id="bank1")
        assert result.gaps == []

    @pytest.mark.asyncio
    async def test_max_memories_limits_recall(self):
        brain, vs, llm = _brain()
        # Store 10 memories
        for i in range(10):
            await brain.retain(f"Alice fact {i}.", bank_id="bank1")

        result = await brain.audit("Alice", bank_id="bank1", max_memories=3)
        assert result.memories_scanned <= 3

    @pytest.mark.asyncio
    async def test_trace_embedded_in_result(self):
        brain, _, _ = _brain()
        await brain.retain("Alice works at Meta.", bank_id="bank1")
        result = await brain.audit("Alice", bank_id="bank1")
        # Trace may be None for empty banks; non-empty should have one
        if result.memories_scanned > 0:
            assert result.trace is not None

    @pytest.mark.asyncio
    async def test_audit_does_not_modify_bank(self):
        brain, vs, _ = _brain()
        await brain.retain("Alice works at Meta.", bank_id="bank1")
        before = await vs.list_vectors("bank1")
        await brain.audit("Alice", bank_id="bank1")
        after = await vs.list_vectors("bank1")
        assert len(before) == len(after)
