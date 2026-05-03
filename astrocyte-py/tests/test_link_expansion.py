"""Tests for the 3-parallel-signal link expansion (Hindsight parity, C3b).

Verifies that all three signals contribute to candidate scoring and
that the orchestrator-side metadata annotations expose which signal
surfaced each candidate.

1. Entity overlap surfaces candidates sharing entities with seeds.
2. Semantic memory_links (precomputed kNN) surface their target.
3. Causal memory_links (caused_by) surface AND get the +1.0 boost.
4. Tag scope filter drops cross-scope candidates.
5. Seeds themselves are excluded from output.
6. ``activation_threshold`` filters low-signal candidates.
7. Multiple signals on the same candidate compound (the score reflects all).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from astrocyte.pipeline.fusion import ScoredItem
from astrocyte.pipeline.link_expansion import (
    LinkExpansionParams,
    link_expansion,
)
from astrocyte.testing.in_memory import (
    InMemoryGraphStore,
    InMemoryVectorStore,
)
from astrocyte.types import (
    Entity,
    MemoryEntityAssociation,
    MemoryLink,
    VectorItem,
)


def _entity(eid: str, name: str) -> Entity:
    return Entity(id=eid, name=name, entity_type="PERSON")


def _vec(vid: str, text: str, *, tags: list[str] | None = None) -> VectorItem:
    return VectorItem(
        id=vid,
        bank_id="b1",
        vector=[1.0] + [0.0] * 127,
        text=text,
        tags=tags or [],
    )


def _mlink(src: str, tgt: str, link_type: str, weight: float = 1.0) -> MemoryLink:
    return MemoryLink(
        source_memory_id=src,
        target_memory_id=tgt,
        link_type=link_type,
        weight=weight,
        confidence=1.0,
        created_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Signal 1: entity overlap
# ---------------------------------------------------------------------------


class TestEntityOverlapSignal:
    @pytest.mark.asyncio
    async def test_candidate_sharing_entity_surfaces(self):
        """Memory sharing an entity with the seed surfaces with
        the entity_overlap signal annotated."""
        gs = InMemoryGraphStore()
        vs = InMemoryVectorStore()
        await gs.store_entities([_entity("alice", "Alice")], "b1")
        await vs.store_vectors([
            _vec("seed-mem", "Alice baked a cake."),
            _vec("linked-mem", "Alice ran a race."),
        ])
        await gs.link_memories_to_entities(
            [
                MemoryEntityAssociation(memory_id="seed-mem", entity_id="alice"),
                MemoryEntityAssociation(memory_id="linked-mem", entity_id="alice"),
            ],
            "b1",
        )

        seed = ScoredItem(id="seed-mem", text="Alice baked a cake.", score=0.9)

        result = await link_expansion(
            [seed], bank_id="b1",
            vector_store=vs, graph_store=gs,
            params=LinkExpansionParams(activation_threshold=0.0),
        )

        ids = [it.id for it in result]
        assert "linked-mem" in ids
        assert "seed-mem" not in ids
        linked = next(it for it in result if it.id == "linked-mem")
        assert "entity_overlap" in linked.metadata["_link_signal"]
        assert linked.metadata["_entity_overlap_count"] == 1


# ---------------------------------------------------------------------------
# Signal 2: semantic memory_links
# ---------------------------------------------------------------------------


class TestSemanticLinkSignal:
    @pytest.mark.asyncio
    async def test_semantic_link_surfaces_target(self):
        """A precomputed ``semantic`` memory link surfaces the target."""
        gs = InMemoryGraphStore()
        vs = InMemoryVectorStore()
        await vs.store_vectors([
            _vec("seed", "x"),
            _vec("similar", "y"),
        ])
        await gs.store_memory_links(
            [_mlink("seed", "similar", "semantic", weight=0.85)], "b1",
        )

        seed = ScoredItem(id="seed", text="x", score=0.9)

        result = await link_expansion(
            [seed], bank_id="b1",
            vector_store=vs, graph_store=gs,
            params=LinkExpansionParams(activation_threshold=0.0),
        )

        ids = {it.id for it in result}
        assert "similar" in ids
        s = next(it for it in result if it.id == "similar")
        assert "semantic" in s.metadata["_link_signal"]
        assert s.metadata["_semantic_weight_total"] == pytest.approx(0.85, abs=1e-3)


# ---------------------------------------------------------------------------
# Signal 3: causal memory_links + +1.0 boost
# ---------------------------------------------------------------------------


class TestCausalLinkSignal:
    @pytest.mark.asyncio
    async def test_causal_link_surfaces_target_with_boost(self):
        """Causal links score with the Hindsight ``+1.0`` boost."""
        gs = InMemoryGraphStore()
        vs = InMemoryVectorStore()
        await vs.store_vectors([
            _vec("effect", "she resigned"),
            _vec("cause", "she was burned out"),
        ])
        # effect ← caused_by ← cause
        await gs.store_memory_links(
            [_mlink("effect", "cause", "caused_by", weight=1.0)], "b1",
        )

        seed = ScoredItem(id="effect", text="she resigned", score=1.0)

        result = await link_expansion(
            [seed], bank_id="b1",
            vector_store=vs, graph_store=gs,
            params=LinkExpansionParams(activation_threshold=0.0),
        )

        ids = {it.id for it in result}
        assert "cause" in ids
        c = next(it for it in result if it.id == "cause")
        assert "causal" in c.metadata["_link_signal"]
        # Causal weight = 1.0 + 1.0 boost = 2.0; metadata shows the raw total.
        assert c.metadata["_causal_weight_total"] == pytest.approx(2.0, abs=1e-3)


# ---------------------------------------------------------------------------
# Compounding signals
# ---------------------------------------------------------------------------


class TestCompoundingSignals:
    @pytest.mark.asyncio
    async def test_candidate_with_multiple_signals_outscores_single_signal(self):
        """A candidate connected by entity_overlap AND a semantic link
        outscores one connected by entity_overlap alone."""
        gs = InMemoryGraphStore()
        vs = InMemoryVectorStore()
        await gs.store_entities([_entity("alice", "Alice")], "b1")
        await vs.store_vectors([
            _vec("seed", "Alice baked a cake."),
            _vec("entity-only", "Alice ran a race."),
            _vec("entity-and-semantic", "Alice climbed a mountain."),
        ])
        await gs.link_memories_to_entities(
            [
                MemoryEntityAssociation(memory_id="seed", entity_id="alice"),
                MemoryEntityAssociation(memory_id="entity-only", entity_id="alice"),
                MemoryEntityAssociation(memory_id="entity-and-semantic", entity_id="alice"),
            ],
            "b1",
        )
        await gs.store_memory_links(
            [_mlink("seed", "entity-and-semantic", "semantic", weight=0.9)], "b1",
        )

        seed = ScoredItem(id="seed", text="Alice baked.", score=0.9)

        result = await link_expansion(
            [seed], bank_id="b1",
            vector_store=vs, graph_store=gs,
            params=LinkExpansionParams(activation_threshold=0.0),
        )
        by_id = {it.id: it for it in result}

        assert "entity-only" in by_id and "entity-and-semantic" in by_id
        assert by_id["entity-and-semantic"].score > by_id["entity-only"].score, (
            "Multi-signal candidate must outscore single-signal"
        )
        assert "semantic" in by_id["entity-and-semantic"].metadata["_link_signal"]
        assert "entity_overlap" in by_id["entity-and-semantic"].metadata["_link_signal"]

    @pytest.mark.asyncio
    async def test_sql_fast_path_rows_are_hydrated_and_scored(self):
        class FastGraphStore(InMemoryGraphStore):
            def __init__(self) -> None:
                super().__init__()
                self.fast_called = False

            async def expand_memory_links_fast(self, seed_memory_ids, bank_id, *, params):
                self.fast_called = True
                assert seed_memory_ids == ["seed"]
                assert bank_id == "b1"
                return [
                    {
                        "memory_id": "fast-candidate",
                        "entity_overlap": 2,
                        "semantic_total": 0.9,
                        "causal_total": 0.0,
                        "sources": ["entity_overlap", "semantic"],
                    }
                ]

        gs = FastGraphStore()
        vs = InMemoryVectorStore()
        await vs.store_vectors([
            _vec("seed", "seed text"),
            _vec("fast-candidate", "hydrated text", tags=["convo:X"]),
        ])

        result = await link_expansion(
            [ScoredItem(id="seed", text="seed text", score=0.9)],
            bank_id="b1",
            vector_store=vs,
            graph_store=gs,
            params=LinkExpansionParams(activation_threshold=0.0),
            tags=["convo:X"],
        )

        assert gs.fast_called is True
        assert [item.id for item in result] == ["fast-candidate"]
        assert result[0].text == "hydrated text"
        assert result[0].metadata["_entity_overlap_count"] == 2
        assert result[0].metadata["_semantic_weight_total"] == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# Filtering & exclusions
# ---------------------------------------------------------------------------


class TestFilteringAndExclusions:
    @pytest.mark.asyncio
    async def test_tag_scope_drops_cross_scope_candidates(self):
        gs = InMemoryGraphStore()
        vs = InMemoryVectorStore()
        await vs.store_vectors([
            _vec("seed", "x", tags=["convo:X"]),
            _vec("in-scope", "y", tags=["convo:X"]),
            _vec("out-of-scope", "z", tags=["convo:Y"]),
        ])
        await gs.store_memory_links(
            [
                _mlink("seed", "in-scope", "semantic", weight=0.8),
                _mlink("seed", "out-of-scope", "semantic", weight=0.8),
            ],
            "b1",
        )

        seed = ScoredItem(id="seed", text="x", score=0.9, tags=["convo:X"])

        result = await link_expansion(
            [seed], bank_id="b1",
            vector_store=vs, graph_store=gs,
            params=LinkExpansionParams(activation_threshold=0.0),
            tags=["convo:X"],
        )

        ids = {it.id for it in result}
        assert "in-scope" in ids
        assert "out-of-scope" not in ids

    @pytest.mark.asyncio
    async def test_seeds_never_appear_in_output(self):
        """A seed cannot be its own candidate."""
        gs = InMemoryGraphStore()
        vs = InMemoryVectorStore()
        await vs.store_vectors([
            _vec("a", "x"),
            _vec("b", "y"),
        ])
        # Mutual semantic link between two seeds.
        await gs.store_memory_links(
            [
                _mlink("a", "b", "semantic", 0.9),
                _mlink("b", "a", "semantic", 0.9),
            ],
            "b1",
        )

        seeds = [
            ScoredItem(id="a", text="x", score=1.0),
            ScoredItem(id="b", text="y", score=0.9),
        ]

        result = await link_expansion(
            seeds, bank_id="b1",
            vector_store=vs, graph_store=gs,
            params=LinkExpansionParams(activation_threshold=0.0),
        )

        # Both are seeds — no candidates should surface.
        assert result == []

    @pytest.mark.asyncio
    async def test_activation_threshold_filters_weak_candidates(self):
        gs = InMemoryGraphStore()
        vs = InMemoryVectorStore()
        await vs.store_vectors([_vec("seed", "x"), _vec("weak", "y")])
        # Weak semantic link (just above zero).
        await gs.store_memory_links(
            [_mlink("seed", "weak", "semantic", weight=0.001)], "b1",
        )

        seed = ScoredItem(id="seed", text="x", score=0.9)

        result = await link_expansion(
            [seed], bank_id="b1",
            vector_store=vs, graph_store=gs,
            params=LinkExpansionParams(activation_threshold=0.5),
        )

        assert result == []
