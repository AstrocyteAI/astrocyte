"""Tests for the native-function-calling agentic reflect loop.

This is the C1c-rewrite of the loop (Hindsight parity). The legacy
JSON-in-prose protocol was replaced with OpenAI/Anthropic-style native
tool calling. Tests script the LLM via :class:`Completion` instances
that carry parsed :class:`ToolCall` objects.

Six behaviors locked in:

1. ``done`` on first turn exits the loop with citations.
2. Multi-turn ``recall`` → ``done`` accumulates evidence.
3. ``search_observations`` tool is offered when ``observations_fn``
   is provided.
4. ``expand`` tool is offered when ``expand_fn`` is provided.
5. Tool-name normalization handles ``functions.recall`` and friends.
6. Provider returning empty ``tool_calls`` falls through to forced
   synthesis (legacy / non-tools providers).
7. ``cited_ids`` referencing unknown IDs are dropped from sources.
"""

from __future__ import annotations

import json

import pytest

from astrocyte.pipeline.agentic_reflect import (
    AgenticReflectParams,
    _normalize_tool_name,
    agentic_reflect,
)
from astrocyte.types import (
    Completion,
    MemoryHit,
    Message,
    ReflectResult,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)


class _ScriptedToolLLM:
    """Provider stub that returns scripted Completion objects with tool_calls.

    Each entry in ``responses`` is either:
    - A ``Completion`` (returned verbatim), or
    - A list of ``(tool_name, args)`` tuples (built into a Completion).
    """

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []  # records what was passed to complete()

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = None,
    ) -> Completion:
        self.calls.append({
            "messages": list(messages),
            "tool_names": [t.name for t in (tools or [])],
            "tool_choice": tool_choice,
        })
        idx = min(len(self.calls) - 1, len(self._responses) - 1)
        item = self._responses[idx]
        if isinstance(item, Completion):
            return item
        # ``item`` is list of (name, args) tuples → build tool_calls.
        tcs = [
            ToolCall(id=f"call-{i}", name=name, arguments=args)
            for i, (name, args) in enumerate(item)
        ]
        return Completion(
            text="",
            model="mock",
            usage=TokenUsage(input_tokens=10, output_tokens=20),
            tool_calls=tcs,
        )


def _hit(mid: str, text: str, score: float = 0.5) -> MemoryHit:
    return MemoryHit(text=text, score=score, memory_id=mid)


class _RecallTracker:
    def __init__(self, hits_per_call: list[list[MemoryHit]]) -> None:
        self._hits_per_call = list(hits_per_call)
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, query: str, max_results: int) -> list[MemoryHit]:
        self.calls.append((query, max_results))
        if not self._hits_per_call:
            return []
        return self._hits_per_call.pop(0)


# ---------------------------------------------------------------------------
# Tool-name normalization
# ---------------------------------------------------------------------------


class TestNormalizeToolName:
    def test_strips_call_prefix(self):
        assert _normalize_tool_name("call=done") == "done"

    def test_strips_functions_prefix(self):
        assert _normalize_tool_name("functions.recall") == "recall"

    def test_strips_call_functions_compound_prefix(self):
        assert _normalize_tool_name("call=functions.done") == "done"

    def test_strips_special_token_suffix(self):
        assert _normalize_tool_name("done<|channel|>commentary") == "done"

    def test_passthrough_for_clean_name(self):
        assert _normalize_tool_name("recall") == "recall"


# ---------------------------------------------------------------------------
# Loop behavior
# ---------------------------------------------------------------------------


class TestAgenticLoop:
    @pytest.mark.asyncio
    async def test_done_first_turn_exits_with_citations(self):
        llm = _ScriptedToolLLM([
            [("done", {"answer": "Caroline went hiking.", "cited_ids": ["m1"]})],
        ])
        recall = _RecallTracker([])

        result = await agentic_reflect(
            "What did Caroline do?",
            initial_hits=[_hit("m1", "Caroline went hiking yesterday.")],
            recall_fn=recall,
            llm_provider=llm,
        )

        assert result.answer == "Caroline went hiking."
        assert [h.memory_id for h in (result.sources or [])] == ["m1"]
        assert recall.calls == []
        assert len(llm.calls) == 1

    @pytest.mark.asyncio
    async def test_recall_then_done_accumulates_evidence(self):
        """Turn 1: recall(refined). Turn 2: done with new ID."""
        llm = _ScriptedToolLLM([
            [("recall", {"reason": "need date", "query": "support group date", "max_results": 5})],
            [("done", {"answer": "On 7 May 2023.", "cited_ids": ["m2"]})],
        ])
        recall = _RecallTracker([
            [_hit("m2", "I went to the LGBTQ support group on 7 May 2023.")]
        ])

        result = await agentic_reflect(
            "When did Caroline go?",
            initial_hits=[_hit("m1", "Caroline mentioned a support group.")],
            recall_fn=recall,
            llm_provider=llm,
        )

        assert result.answer == "On 7 May 2023."
        assert len(recall.calls) == 1
        assert recall.calls[0] == ("support group date", 5)
        assert [h.memory_id for h in (result.sources or [])] == ["m2"]

    @pytest.mark.asyncio
    async def test_recall_default_max_results_when_omitted(self):
        """If the model doesn't supply ``max_results``, the loop falls
        back to ``params.recall_step_max_results``."""
        llm = _ScriptedToolLLM([
            [("recall", {"reason": "broaden", "query": "Caroline"})],
            [("done", {"answer": "x", "cited_ids": []})],
        ])
        recall = _RecallTracker([[]])
        params = AgenticReflectParams(recall_step_max_results=7)

        await agentic_reflect(
            "q", initial_hits=[], recall_fn=recall, llm_provider=llm, params=params,
        )

        assert recall.calls[0][1] == 7

    @pytest.mark.asyncio
    async def test_observations_tool_offered_when_callback_provided(self):
        """``search_observations`` is in the tool list iff
        ``observations_fn`` is non-None."""
        llm = _ScriptedToolLLM([
            [("done", {"answer": "x", "cited_ids": []})],
        ])

        async def obs_fn(q: str, n: int) -> list[MemoryHit]:
            return []

        await agentic_reflect(
            "q", initial_hits=[],
            recall_fn=_RecallTracker([]),
            observations_fn=obs_fn,
            llm_provider=llm,
        )

        offered = llm.calls[0]["tool_names"]
        assert "search_observations" in offered
        assert "recall" in offered
        assert "done" in offered

    @pytest.mark.asyncio
    async def test_observations_tool_not_offered_when_callback_absent(self):
        llm = _ScriptedToolLLM([
            [("done", {"answer": "x", "cited_ids": []})],
        ])

        await agentic_reflect(
            "q", initial_hits=[],
            recall_fn=_RecallTracker([]),
            llm_provider=llm,
        )

        offered = llm.calls[0]["tool_names"]
        assert "search_observations" not in offered

    @pytest.mark.asyncio
    async def test_mental_models_tool_offered_when_callback_provided(self):
        """``search_mental_models`` is in the tool list iff
        ``mental_models_fn`` is non-None.  Tool order matters: it's the
        FIRST tool in the priority hierarchy (mental_models → observations
        → recall → expand → done)."""
        llm = _ScriptedToolLLM([
            [("done", {"answer": "x", "cited_ids": []})],
        ])

        async def mm_fn(q: str, scope: str | None) -> list[MemoryHit]:
            return []

        await agentic_reflect(
            "q", initial_hits=[],
            recall_fn=_RecallTracker([]),
            mental_models_fn=mm_fn,
            llm_provider=llm,
        )

        offered = llm.calls[0]["tool_names"]
        assert "search_mental_models" in offered
        assert "recall" in offered
        assert "done" in offered
        # Priority order: mental_models is offered before recall.
        assert offered.index("search_mental_models") < offered.index("recall")

    @pytest.mark.asyncio
    async def test_mental_models_tool_not_offered_when_callback_absent(self):
        llm = _ScriptedToolLLM([
            [("done", {"answer": "x", "cited_ids": []})],
        ])

        await agentic_reflect(
            "q", initial_hits=[],
            recall_fn=_RecallTracker([]),
            llm_provider=llm,
        )

        offered = llm.calls[0]["tool_names"]
        assert "search_mental_models" not in offered

    @pytest.mark.asyncio
    async def test_mental_models_tool_routes_query_and_scope(self):
        """The model picks search_mental_models → the loop forwards
        (query, scope) to mental_models_fn; results join the evidence pool
        and become citable."""
        llm = _ScriptedToolLLM([
            [(
                "search_mental_models",
                {"reason": "stable preference", "query": "alice prefs", "scope": "person:alice"},
            )],
            [("done", {"answer": "Alice prefers async.", "cited_ids": ["mm-1"]})],
        ])

        mm_calls: list[tuple[str, str | None]] = []

        async def mm_fn(query: str, scope: str | None) -> list[MemoryHit]:
            mm_calls.append((query, scope))
            return [_hit("mm-1", "Alice prefers async communication.")]

        result = await agentic_reflect(
            "q",
            initial_hits=[],
            recall_fn=_RecallTracker([]),
            mental_models_fn=mm_fn,
            llm_provider=llm,
        )

        assert mm_calls == [("alice prefs", "person:alice")]
        assert result.answer == "Alice prefers async."
        assert "mm-1" in {h.memory_id for h in (result.sources or [])}

    @pytest.mark.asyncio
    async def test_expand_tool_routes_to_expand_fn(self):
        """The model picks expand → loop calls ``expand_fn`` with the
        memory_id; new evidence joins the pool."""
        llm = _ScriptedToolLLM([
            [("expand", {"reason": "need source", "memory_id": "obs-1", "max_sources": 3})],
            [("done", {"answer": "from source", "cited_ids": ["raw-a"]})],
        ])

        expand_calls: list[tuple[str, int]] = []

        async def expand_fn(memory_id: str, max_sources: int) -> list[MemoryHit]:
            expand_calls.append((memory_id, max_sources))
            return [_hit("raw-a", "raw evidence cited by obs-1")]

        result = await agentic_reflect(
            "q",
            initial_hits=[_hit("obs-1", "compiled summary")],
            recall_fn=_RecallTracker([]),
            expand_fn=expand_fn,
            llm_provider=llm,
        )

        assert expand_calls == [("obs-1", 3)]
        assert "raw-a" in {h.memory_id for h in (result.sources or [])}

    @pytest.mark.asyncio
    async def test_normalized_tool_name_dispatches_correctly(self):
        """``functions.done`` (with prefix) routes to the done handler."""
        llm = _ScriptedToolLLM([
            [("functions.done", {"answer": "ok", "cited_ids": ["m1"]})],
        ])
        result = await agentic_reflect(
            "q",
            initial_hits=[_hit("m1", "evidence")],
            recall_fn=_RecallTracker([]),
            llm_provider=llm,
        )

        assert result.answer == "ok"
        assert [h.memory_id for h in (result.sources or [])] == ["m1"]

    @pytest.mark.asyncio
    async def test_unknown_cited_id_dropped_from_sources(self):
        llm = _ScriptedToolLLM([
            [("done", {"answer": "...", "cited_ids": ["m1", "imaginary"]})],
        ])
        result = await agentic_reflect(
            "q",
            initial_hits=[_hit("m1", "real")],
            recall_fn=_RecallTracker([]),
            llm_provider=llm,
        )

        ids = {h.memory_id for h in (result.sources or [])}
        assert "m1" in ids
        assert "imaginary" not in ids

    @pytest.mark.asyncio
    async def test_no_tool_calls_falls_back_to_forced_synthesis(self):
        """A provider that returns plain text (no tool_calls) — typical
        of legacy providers without tool-calling support — triggers the
        forced-synthesis fallback path."""
        # Plain Completion with no tool_calls field set.
        llm = _ScriptedToolLLM([
            Completion(text="prose response", model="mock", usage=None, tool_calls=None)
        ])

        synth_called = {}

        async def fake_synth(*, query, hits, llm_provider, **kwargs):
            synth_called["called"] = True
            return ReflectResult(answer="forced fallback", sources=hits)

        result = await agentic_reflect(
            "q",
            initial_hits=[_hit("m1", "x")],
            recall_fn=_RecallTracker([]),
            llm_provider=llm,
            final_synthesize_fn=fake_synth,
        )

        assert synth_called.get("called") is True
        assert result.answer == "forced fallback"

    @pytest.mark.asyncio
    async def test_max_iterations_falls_back_to_forced_synthesis(self):
        """Loop hits the cap without ``done`` — forced synth runs over
        the accumulated evidence."""
        llm = _ScriptedToolLLM([
            [("recall", {"reason": "1", "query": "a", "max_results": 5})],
            [("recall", {"reason": "2", "query": "b", "max_results": 5})],
            [("recall", {"reason": "3", "query": "c", "max_results": 5})],
        ])
        recall = _RecallTracker([
            [_hit("m2", "two")],
            [_hit("m3", "three")],
            [_hit("m4", "four")],
        ])

        synth_called = {}

        async def fake_synth(*, query, hits, llm_provider, **kwargs):
            synth_called["hit_ids"] = [h.memory_id for h in hits]
            return ReflectResult(answer="forced", sources=hits)

        result = await agentic_reflect(
            "q",
            initial_hits=[_hit("m1", "seed")],
            recall_fn=recall,
            llm_provider=llm,
            params=AgenticReflectParams(max_iterations=3),
            final_synthesize_fn=fake_synth,
        )

        assert result.answer == "forced"
        assert synth_called["hit_ids"] == ["m1", "m2", "m3", "m4"]

    @pytest.mark.asyncio
    async def test_adversarial_defense_appends_rules_to_system_prompt(self):
        """When ``adversarial_defense=True``, the system prompt seen by the
        provider includes the explicit premise-check / negative-existence /
        time-shift / "insufficient evidence is always valid" rules."""
        llm = _ScriptedToolLLM([
            [("done", {"answer": "x", "cited_ids": []})],
        ])

        await agentic_reflect(
            "q",
            initial_hits=[],
            recall_fn=_RecallTracker([]),
            llm_provider=llm,
            params=AgenticReflectParams(adversarial_defense=True),
        )

        sys_msg = next(
            m for m in llm.calls[0]["messages"] if m.role == "system"
        )
        assert "ADVERSARIAL DEFENSE" in sys_msg.content
        assert "presupposes" in sys_msg.content.lower()
        assert "insufficient evidence" in sys_msg.content.lower()

    @pytest.mark.asyncio
    async def test_adversarial_defense_off_keeps_base_prompt(self):
        """Default (``adversarial_defense=False``) does NOT inject the
        defense rules — keeps the prompt lean for non-adversarial workloads."""
        llm = _ScriptedToolLLM([
            [("done", {"answer": "x", "cited_ids": []})],
        ])

        await agentic_reflect(
            "q",
            initial_hits=[],
            recall_fn=_RecallTracker([]),
            llm_provider=llm,
        )

        sys_msg = next(
            m for m in llm.calls[0]["messages"] if m.role == "system"
        )
        assert "ADVERSARIAL DEFENSE" not in sys_msg.content

    @pytest.mark.asyncio
    async def test_base_prompt_includes_extraction_discipline(self):
        """The base system prompt MUST include the extraction-discipline
        rules so the agent extracts specific facts from retrieved evidence
        instead of reflexively saying "insufficient evidence" when
        evidence is partial. Lock this in: 2026-05-02 LoCoMo bench
        showed ~36 of 90 synth misses (40%) were over-cautious abstains
        on questions where the answer WAS in recall hits (e.g. "James's
        favorite game" with recall containing "James: my favorite game
        is Apex Legends" yet the model returned 'insufficient evidence')."""
        llm = _ScriptedToolLLM([
            [("done", {"answer": "x", "cited_ids": []})],
        ])

        await agentic_reflect(
            "q",
            initial_hits=[],
            recall_fn=_RecallTracker([]),
            llm_provider=llm,
            # No adversarial_defense — verifying base prompt content
        )

        sys_msg = next(
            m for m in llm.calls[0]["messages"] if m.role == "system"
        )
        c = sys_msg.content
        # Required content for the extraction-discipline guard
        assert "EXTRACTION DISCIPLINE" in c, (
            "Base prompt must contain the EXTRACTION DISCIPLINE section."
        )
        assert "MUST extract" in c
        # Must enumerate INVALID reasons to abstain (the bug we saw)
        assert "wording in evidence differs" in c.lower()
        assert "partial information" in c.lower()
        # Must constrain when "insufficient evidence" IS valid
        assert "ONLY valid when" in c

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error_to_model(self):
        """An unknown tool name is fed back as a structured error, then
        the model gets another turn to recover (call done in this case)."""
        llm = _ScriptedToolLLM([
            [("delete_database", {"sneaky": "yes"})],
            [("done", {"answer": "recovered", "cited_ids": []})],
        ])

        result = await agentic_reflect(
            "q",
            initial_hits=[],
            recall_fn=_RecallTracker([]),
            llm_provider=llm,
        )

        assert result.answer == "recovered"
        # The 2nd LLM call must have seen a tool-error message.
        assert len(llm.calls) == 2
        last_messages = llm.calls[1]["messages"]
        tool_msgs = [m for m in last_messages if m.role == "tool"]
        assert any("unknown tool" in str(m.content) for m in tool_msgs)


# ---------------------------------------------------------------------------
# Temporal-resolution surfacing
# ---------------------------------------------------------------------------


class TestResolvedTemporalSurfacing:
    """``_format_hits_for_tool_response`` must surface the pre-computed
    ``resolved_date`` / ``temporal_phrase`` fields to the agent so it
    quotes the canonical date instead of redoing relative arithmetic.

    The bug this guards: a memory with text "Caroline: yesterday I went
    to the LGBTQ support group" and metadata {"resolved_date": "2023-05-07",
    "temporal_phrase": "yesterday"} surfaces only `text` to the agent,
    which then redoes "yesterday" against an assumed reference and lands
    on a different date.
    """

    def test_resolved_temporal_field_appears_in_payload(self):
        from astrocyte.pipeline.agentic_reflect import _format_hits_for_tool_response

        hit = MemoryHit(
            text="Caroline: yesterday I went to the LGBTQ support group.",
            score=0.8,
            memory_id="m-locomo-c1-s3-t14",
            metadata={
                "temporal_anchor": "2023-05-08",
                "temporal_phrase": "yesterday",
                "resolved_date": "2023-05-07",
                "date_granularity": "day",
            },
        )
        payload_str = _format_hits_for_tool_response([hit])
        payload = json.loads(payload_str)
        assert len(payload) == 1
        entry = payload[0]
        assert entry["id"] == "m-locomo-c1-s3-t14"
        assert "resolved_temporal" in entry
        rt = entry["resolved_temporal"]
        assert rt["anchor_date"] == "2023-05-08"
        assert rt["items"] == [
            {"phrase": "yesterday", "resolved_date": "2023-05-07", "granularity": "day"}
        ]

    def test_resolved_temporal_omitted_when_absent(self):
        from astrocyte.pipeline.agentic_reflect import _format_hits_for_tool_response

        hit = MemoryHit(text="Plain text, no temporal facts.", score=0.5, memory_id="m1")
        payload = json.loads(_format_hits_for_tool_response([hit]))
        assert "resolved_temporal" not in payload[0]

    def test_resolved_temporal_omitted_when_only_one_side_present(self):
        """Half-populated temporal metadata (phrase without resolved_date,
        or vice versa) is treated as missing — never partial output."""
        from astrocyte.pipeline.agentic_reflect import _format_hits_for_tool_response

        hit_phrase_only = MemoryHit(
            text="x", score=0.5, memory_id="m1",
            metadata={"temporal_phrase": "yesterday"},
        )
        hit_date_only = MemoryHit(
            text="x", score=0.5, memory_id="m2",
            metadata={"resolved_date": "2023-05-07"},
        )
        for hit in (hit_phrase_only, hit_date_only):
            payload = json.loads(_format_hits_for_tool_response([hit]))
            assert "resolved_temporal" not in payload[0]

    def test_multi_phrase_pipe_joined_metadata_round_trips_to_lists(self):
        """temporal_metadata() joins multiple phrases per session with ``|``;
        the formatter must split them back into a parallel list."""
        from astrocyte.pipeline.agentic_reflect import _format_hits_for_tool_response

        hit = MemoryHit(
            text="...", score=0.5, memory_id="m1",
            metadata={
                "temporal_anchor": "2023-05-08",
                "temporal_phrase": "yesterday|last week",
                "resolved_date": "2023-05-07|2023-05-01",
                "date_granularity": "day|week",
            },
        )
        payload = json.loads(_format_hits_for_tool_response([hit]))
        items = payload[0]["resolved_temporal"]["items"]
        assert items == [
            {"phrase": "yesterday", "resolved_date": "2023-05-07", "granularity": "day"},
            {"phrase": "last week", "resolved_date": "2023-05-01", "granularity": "week"},
        ]

    def test_system_prompt_documents_resolved_temporal_field(self):
        """The agent must be told to USE resolved_temporal verbatim
        instead of redoing arithmetic from raw text."""
        from astrocyte.pipeline.agentic_reflect import _build_system_prompt

        prompt = _build_system_prompt(adversarial_defense=False)
        assert "resolved_temporal" in prompt
        assert "USE THE DATES FROM THAT FIELD VERBATIM" in prompt
        # And the off-by-one warning is explicit.
        assert "off-by-one" in prompt
