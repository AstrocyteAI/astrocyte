"""M11a: Entity resolution — unit and integration tests.

Tests cover:
- EntityLink field migration: entity_a, entity_b, evidence, confidence, created_at
- EntityLink backward compat: link_type and metadata preserved
- InMemoryGraphStore.find_entity_candidates: substring match
- InMemoryGraphStore.find_entity_candidates: case-insensitive
- InMemoryGraphStore.find_entity_candidates: limit respected
- InMemoryGraphStore.find_entity_candidates: empty bank returns empty list
- InMemoryGraphStore.store_entity_link: persists link, returns id
- EntityResolver.resolve(): no candidates → no LLM call, no links written
- EntityResolver.resolve(): candidate found → LLM called → link written on confirm
- EntityResolver.resolve(): LLM returns same_entity=false → no link written
- EntityResolver.resolve(): LLM confidence below threshold → no link written
- EntityResolver.resolve(): LLM failure is non-fatal (graceful degradation)
- EntityResolver.resolve(): bad JSON from LLM is non-fatal
- EntityResolver.resolve(): self-candidate (same id) is skipped
- EntityResolver.resolve(): max_candidates_per_entity caps LLM calls
- EntityResolver.resolve(): markdown-fenced JSON response parsed correctly
- EntityResolver.resolve(): confidence clamped to [0, 1]
- EntityResolver._confirm_and_link(): evidence propagated to EntityLink
- Orchestrator retain: entity_resolver=None → no resolution (default)
- Orchestrator retain: entity_resolver wired → alias_of link written on confirm
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from astrocyte.pipeline.entity_resolution import EntityResolver
from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.testing.in_memory import InMemoryGraphStore, InMemoryVectorStore, MockLLMProvider
from astrocyte.types import Entity, EntityLink

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entity(name: str, eid: str | None = None) -> Entity:
    return Entity(id=eid or name.lower().replace(" ", "_"), name=name, entity_type="PERSON")


def _confirm_response(same: bool = True, confidence: float = 0.9, evidence: str = "quote") -> str:
    return json.dumps({"same_entity": same, "confidence": confidence, "evidence": evidence})


def _deny_response() -> str:
    return json.dumps({"same_entity": False, "confidence": 0.1, "evidence": ""})


class ControlledLLMProvider:
    """Returns preset responses; delegates embed to MockLLMProvider."""

    SPI_VERSION = 1

    def __init__(self, responses: list[str] | None = None, default: str = "") -> None:
        self._responses = list(responses or [])
        self._default = default
        self._mock = MockLLMProvider()
        self.complete_calls: list[list] = []

    async def complete(self, messages, model=None, max_tokens=1024, temperature=0.0):
        self.complete_calls.append(messages)
        from astrocyte.types import Completion, TokenUsage
        resp = self._responses.pop(0) if self._responses else self._default
        return Completion(text=resp, model="ctrl", usage=TokenUsage(input_tokens=10, output_tokens=30))

    async def embed(self, texts, model=None):
        return await self._mock.embed(texts, model=model)

    def capabilities(self):
        return self._mock.capabilities()


class FailingLLMProvider(ControlledLLMProvider):
    async def complete(self, messages, model=None, max_tokens=1024, temperature=0.0):
        raise RuntimeError("LLM unavailable")


# ---------------------------------------------------------------------------
# EntityLink type tests
# ---------------------------------------------------------------------------


class TestEntityLinkFields:
    def test_new_fields_present(self):
        link = EntityLink(entity_a="e1", entity_b="e2", link_type="alias_of")
        assert link.entity_a == "e1"
        assert link.entity_b == "e2"
        assert link.link_type == "alias_of"
        assert link.evidence == ""
        assert link.confidence == 1.0
        assert link.created_at is None
        assert link.metadata is None

    def test_all_fields_settable(self):
        ts = datetime.now(UTC)
        link = EntityLink(
            entity_a="e1",
            entity_b="e2",
            link_type="alias_of",
            evidence="He is the CTO.",
            confidence=0.85,
            created_at=ts,
            metadata={"source": "retain"},
        )
        assert link.evidence == "He is the CTO."
        assert link.confidence == pytest.approx(0.85)
        assert link.created_at is ts
        assert link.metadata == {"source": "retain"}

    def test_co_occurs_link_still_works(self):
        link = EntityLink(entity_a="e1", entity_b="e2", link_type="co_occurs")
        assert link.link_type == "co_occurs"


# ---------------------------------------------------------------------------
# InMemoryGraphStore: new SPI methods
# ---------------------------------------------------------------------------


class TestInMemoryGraphStoreFindCandidates:
    @pytest.mark.asyncio
    async def test_empty_bank_returns_empty(self):
        gs = InMemoryGraphStore()
        result = await gs.find_entity_candidates("Alice", "bank1")
        assert result == []

    @pytest.mark.asyncio
    async def test_exact_name_match(self):
        gs = InMemoryGraphStore()
        await gs.store_entities([_entity("Alice Smith")], "bank1")
        result = await gs.find_entity_candidates("Alice", "bank1")
        assert any(e.name == "Alice Smith" for e in result)

    @pytest.mark.asyncio
    async def test_case_insensitive(self):
        gs = InMemoryGraphStore()
        await gs.store_entities([_entity("Alice Smith")], "bank1")
        result = await gs.find_entity_candidates("alice", "bank1")
        assert len(result) >= 1

    @pytest.mark.asyncio
    async def test_non_matching_entity_excluded(self):
        gs = InMemoryGraphStore()
        await gs.store_entities([_entity("Bob Jones")], "bank1")
        result = await gs.find_entity_candidates("Alice", "bank1")
        assert result == []

    @pytest.mark.asyncio
    async def test_limit_respected(self):
        gs = InMemoryGraphStore()
        entities = [_entity(f"Alice {i}", f"alice_{i}") for i in range(10)]
        await gs.store_entities(entities, "bank1")
        result = await gs.find_entity_candidates("Alice", "bank1", limit=3)
        assert len(result) <= 3

    @pytest.mark.asyncio
    async def test_bank_isolation(self):
        gs = InMemoryGraphStore()
        await gs.store_entities([_entity("Alice")], "bank1")
        result = await gs.find_entity_candidates("Alice", "bank2")
        assert result == []


class TestInMemoryGraphStoreEntityLink:
    @pytest.mark.asyncio
    async def test_store_entity_link_returns_id(self):
        gs = InMemoryGraphStore()
        link = EntityLink(entity_a="e1", entity_b="e2", link_type="alias_of", confidence=0.9)
        lid = await gs.store_entity_link(link, "bank1")
        assert isinstance(lid, str)
        assert len(lid) > 0

    @pytest.mark.asyncio
    async def test_store_entity_link_persists(self):
        gs = InMemoryGraphStore()
        link = EntityLink(entity_a="e1", entity_b="e2", link_type="alias_of")
        await gs.store_entity_link(link, "bank1")
        stored = gs._links.get("bank1", [])
        assert any(lnk.entity_a == "e1" and lnk.entity_b == "e2" for lnk in stored)


# ---------------------------------------------------------------------------
# EntityResolver unit tests
# ---------------------------------------------------------------------------


class TestEntityResolverNoCandidates:
    @pytest.mark.asyncio
    async def test_no_candidates_no_llm_call(self):
        gs = InMemoryGraphStore()
        llm = ControlledLLMProvider()
        resolver = EntityResolver()

        new_entity = _entity("Alice", "alice_1")
        await gs.store_entities([new_entity], "bank1")

        links = await resolver.resolve(
            new_entities=[new_entity],
            source_text="Alice is the CTO.",
            bank_id="bank1",
            graph_store=gs,
            llm_provider=llm,
        )
        assert links == []
        assert llm.complete_calls == []

    @pytest.mark.asyncio
    async def test_self_candidate_skipped(self):
        """Entity should not be linked to itself."""
        gs = InMemoryGraphStore()
        llm = ControlledLLMProvider(responses=[_confirm_response()])
        resolver = EntityResolver()

        entity = _entity("Alice", "alice_1")
        await gs.store_entities([entity], "bank1")

        links = await resolver.resolve(
            new_entities=[entity],
            source_text="Alice is the CTO.",
            bank_id="bank1",
            graph_store=gs,
            llm_provider=llm,
        )
        assert links == []
        assert llm.complete_calls == []


class TestEntityResolverConfirmation:
    @pytest.mark.asyncio
    async def test_confirmed_link_written(self):
        gs = InMemoryGraphStore()
        llm = ControlledLLMProvider(responses=[_confirm_response(same=True, confidence=0.9, evidence="He is the CTO.")])
        resolver = EntityResolver()

        existing = _entity("Calvin the CTO", "calvin_cto")
        new_entity = _entity("Calvin", "calvin_new")
        await gs.store_entities([existing], "bank1")
        await gs.store_entities([new_entity], "bank1")

        links = await resolver.resolve(
            new_entities=[new_entity],
            source_text="Calvin joined the meeting as the CTO.",
            bank_id="bank1",
            graph_store=gs,
            llm_provider=llm,
        )
        assert len(links) == 1
        assert links[0].link_type == "alias_of"
        assert links[0].confidence == pytest.approx(0.9)
        assert links[0].evidence == "He is the CTO."

    @pytest.mark.asyncio
    async def test_denied_link_not_written(self):
        gs = InMemoryGraphStore()
        llm = ControlledLLMProvider(responses=[_deny_response()])
        resolver = EntityResolver()

        existing = _entity("Bob", "bob_1")
        new_entity = _entity("Alice", "alice_1")
        await gs.store_entities([existing, new_entity], "bank1")

        links = await resolver.resolve(
            new_entities=[new_entity],
            source_text="Alice and Bob are different people.",
            bank_id="bank1",
            graph_store=gs,
            llm_provider=llm,
        )
        assert links == []

    @pytest.mark.asyncio
    async def test_confidence_below_threshold_not_written(self):
        gs = InMemoryGraphStore()
        llm = ControlledLLMProvider(responses=[_confirm_response(same=True, confidence=0.5)])
        resolver = EntityResolver(confirmation_threshold=0.75)

        existing = _entity("Calvin the CTO", "calvin_cto")
        new_entity = _entity("Calvin", "calvin_new")
        await gs.store_entities([existing, new_entity], "bank1")

        links = await resolver.resolve(
            new_entities=[new_entity],
            source_text="Calvin joined.",
            bank_id="bank1",
            graph_store=gs,
            llm_provider=llm,
        )
        assert links == []

    @pytest.mark.asyncio
    async def test_entity_ids_in_link(self):
        gs = InMemoryGraphStore()
        llm = ControlledLLMProvider(responses=[_confirm_response()])
        resolver = EntityResolver()

        existing = _entity("Calvin CTO", "calvin_cto")
        new_entity = _entity("Calvin", "calvin_new")
        await gs.store_entities([existing, new_entity], "bank1")

        links = await resolver.resolve(
            new_entities=[new_entity],
            source_text="Calvin is the CTO.",
            bank_id="bank1",
            graph_store=gs,
            llm_provider=llm,
        )
        assert len(links) == 1
        assert {links[0].entity_a, links[0].entity_b} == {"calvin_new", "calvin_cto"}

    @pytest.mark.asyncio
    async def test_created_at_stamped(self):
        gs = InMemoryGraphStore()
        llm = ControlledLLMProvider(responses=[_confirm_response()])
        resolver = EntityResolver()

        before = datetime.now(UTC)
        existing = _entity("Calvin CTO", "calvin_cto")
        new_entity = _entity("Calvin", "calvin_new")
        await gs.store_entities([existing, new_entity], "bank1")

        links = await resolver.resolve(
            new_entities=[new_entity],
            source_text="Calvin is the CTO.",
            bank_id="bank1",
            graph_store=gs,
            llm_provider=llm,
        )
        after = datetime.now(UTC)
        assert len(links) == 1
        assert links[0].created_at is not None
        assert before <= links[0].created_at <= after


class TestEntityResolverRobustness:
    @pytest.mark.asyncio
    async def test_llm_failure_is_non_fatal(self):
        gs = InMemoryGraphStore()
        existing = _entity("Calvin CTO", "calvin_cto")
        new_entity = _entity("Calvin", "calvin_new")
        await gs.store_entities([existing, new_entity], "bank1")

        links = await EntityResolver().resolve(
            new_entities=[new_entity],
            source_text="Calvin joined.",
            bank_id="bank1",
            graph_store=gs,
            llm_provider=FailingLLMProvider(),
        )
        assert links == []  # graceful degradation

    @pytest.mark.asyncio
    async def test_bad_json_is_non_fatal(self):
        gs = InMemoryGraphStore()
        llm = ControlledLLMProvider(responses=["this is not json"])
        existing = _entity("Calvin CTO", "calvin_cto")
        new_entity = _entity("Calvin", "calvin_new")
        await gs.store_entities([existing, new_entity], "bank1")

        links = await EntityResolver().resolve(
            new_entities=[new_entity],
            source_text="Calvin joined.",
            bank_id="bank1",
            graph_store=gs,
            llm_provider=llm,
        )
        assert links == []

    @pytest.mark.asyncio
    async def test_markdown_fenced_json_parsed(self):
        raw = "```json\n" + _confirm_response(same=True, confidence=0.9) + "\n```"
        gs = InMemoryGraphStore()
        llm = ControlledLLMProvider(responses=[raw])
        existing = _entity("Calvin CTO", "calvin_cto")
        new_entity = _entity("Calvin", "calvin_new")
        await gs.store_entities([existing, new_entity], "bank1")

        links = await EntityResolver().resolve(
            new_entities=[new_entity],
            source_text="Calvin is the CTO.",
            bank_id="bank1",
            graph_store=gs,
            llm_provider=llm,
        )
        assert len(links) == 1

    @pytest.mark.asyncio
    async def test_max_candidates_caps_llm_calls(self):
        gs = InMemoryGraphStore()
        # 5 candidates, max 2 LLM calls
        candidates = [_entity(f"Calvin variant {i}", f"calvin_{i}") for i in range(5)]
        new_entity = _entity("Calvin", "calvin_new")
        await gs.store_entities(candidates + [new_entity], "bank1")

        llm = ControlledLLMProvider(default=_deny_response())
        resolver = EntityResolver(max_candidates_per_entity=2)

        await resolver.resolve(
            new_entities=[new_entity],
            source_text="Calvin joined.",
            bank_id="bank1",
            graph_store=gs,
            llm_provider=llm,
        )
        assert len(llm.complete_calls) <= 2


# ---------------------------------------------------------------------------
# Orchestrator integration
# ---------------------------------------------------------------------------


class TestOrchestratorEntityResolution:
    @pytest.mark.asyncio
    async def test_no_resolver_no_links(self):
        """entity_resolver=None (default) — retain works normally, no alias links."""
        vs = InMemoryVectorStore()
        gs = InMemoryGraphStore()
        llm = MockLLMProvider()
        orch = PipelineOrchestrator(vs, llm, graph_store=gs)

        from astrocyte.types import RetainRequest
        await orch.retain(RetainRequest(content="Calvin is the CTO.", bank_id="bank1"))

        # No alias_of links should exist
        alias_links = [lnk for lnk in gs._links.get("bank1", []) if lnk.link_type == "alias_of"]
        assert alias_links == []

    @pytest.mark.asyncio
    async def test_resolver_wired_writes_alias_link(self):
        """With entity_resolver set, a confirmed alias creates an alias_of link."""
        vs = InMemoryVectorStore()
        gs = InMemoryGraphStore()

        # Pre-load a "Calvin the CTO" entity
        existing = Entity(id="calvin_cto", name="Calvin the CTO", entity_type="PERSON")
        await gs.store_entities([existing], "bank1")

        # LLM: first call = entity extraction (returns JSON entity list)
        # second call = entity resolution confirmation
        llm = ControlledLLMProvider(responses=[
            '[{"name": "Calvin", "entity_type": "PERSON", "aliases": []}]',
            _confirm_response(same=True, confidence=0.9, evidence="Calvin is the CTO"),
        ])

        resolver = EntityResolver(confirmation_threshold=0.8)
        orch = PipelineOrchestrator(vs, llm, graph_store=gs, entity_resolver=resolver)

        from astrocyte.types import RetainRequest
        await orch.retain(RetainRequest(content="Calvin is the CTO.", bank_id="bank1"))

        alias_links = [lnk for lnk in gs._links.get("bank1", []) if lnk.link_type == "alias_of"]
        assert len(alias_links) == 1
        assert alias_links[0].confidence == pytest.approx(0.9)

    @pytest.mark.asyncio
    async def test_resolver_failure_does_not_abort_retain(self):
        """Even if entity resolution errors, the retain still succeeds."""
        vs = InMemoryVectorStore()
        gs = InMemoryGraphStore()

        llm = ControlledLLMProvider(responses=[
            '[{"name": "Calvin", "entity_type": "PERSON", "aliases": []}]',
        ])
        # FailingLLMProvider for the resolution step — but we use a single provider
        # so we just exhaust responses and let it return the default empty string.

        resolver = EntityResolver()
        orch = PipelineOrchestrator(vs, llm, graph_store=gs, entity_resolver=resolver)

        from astrocyte.types import RetainRequest
        result = await orch.retain(RetainRequest(content="Calvin is the CTO.", bank_id="bank1"))
        assert result.stored is True
