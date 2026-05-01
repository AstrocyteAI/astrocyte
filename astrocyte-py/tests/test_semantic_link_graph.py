"""Tests for the precomputed semantic-kNN graph (Hindsight parity, C3a).

Five behaviors locked in:

1. Each new memory gets up to ``top_k`` neighbors above ``similarity_threshold``.
2. Self-exclusion: a memory never links to itself.
3. Same-batch exclusion: memories in the same retain batch don't link
   to each other (the causal_by signal is the right channel for that).
4. Below-threshold neighbors are dropped.
5. Empty inputs short-circuit cleanly.
"""

from __future__ import annotations

import pytest

from astrocyte.pipeline.semantic_link_graph import compute_semantic_links
from astrocyte.testing.in_memory import InMemoryVectorStore
from astrocyte.types import VectorItem


def _vec_at(*positions: float) -> list[float]:
    """Build a 128-dim vector with ``positions[i]`` at index i."""
    v = [0.0] * 128
    for i, val in enumerate(positions):
        v[i] = val
    return v


class TestComputeSemanticLinks:
    @pytest.mark.asyncio
    async def test_links_new_memory_to_existing_above_threshold(self):
        """A new memory similar to several existing memories gets edges
        to each above the threshold, ranked by similarity."""
        vs = InMemoryVectorStore()
        # Existing population — three vectors close to (1, 0, 0).
        await vs.store_vectors([
            VectorItem(
                id=f"existing-{i}",
                bank_id="b1",
                vector=_vec_at(1.0, float(i) * 0.01),
                text=f"existing memory {i}",
            )
            for i in range(3)
        ])

        # New memory close to those.
        new_id = "new-1"
        new_vec = _vec_at(1.0, 0.0)

        links = await compute_semantic_links(
            bank_id="b1",
            new_memory_ids=[new_id],
            new_embeddings=[new_vec],
            vector_store=vs,
            top_k=5,
            similarity_threshold=0.7,
        )

        assert len(links) >= 1
        for link in links:
            assert link.source_memory_id == "new-1"
            assert link.target_memory_id.startswith("existing-")
            assert link.link_type == "semantic"
            assert link.weight >= 0.7

    @pytest.mark.asyncio
    async def test_self_exclusion(self):
        """A memory's kNN must never include itself, even when search
        returns it (which happens when the new memory is already stored)."""
        vs = InMemoryVectorStore()
        await vs.store_vectors([
            VectorItem(id="m-self", bank_id="b1", vector=_vec_at(1.0, 0.0), text="x"),
            VectorItem(id="m-other", bank_id="b1", vector=_vec_at(1.0, 0.05), text="y"),
        ])

        links = await compute_semantic_links(
            bank_id="b1",
            new_memory_ids=["m-self"],
            new_embeddings=[_vec_at(1.0, 0.0)],
            vector_store=vs,
            top_k=5,
            similarity_threshold=0.5,
        )

        target_ids = {link.target_memory_id for link in links}
        assert "m-self" not in target_ids
        assert "m-other" in target_ids

    @pytest.mark.asyncio
    async def test_same_batch_exclusion(self):
        """Memories created in the same retain batch don't link to each
        other — the causal/co-occurs signal is the right channel for that."""
        vs = InMemoryVectorStore()
        await vs.store_vectors([
            VectorItem(id="m-existing", bank_id="b1", vector=_vec_at(1.0, 0.0), text="x"),
            VectorItem(id="m-batch-A", bank_id="b1", vector=_vec_at(1.0, 0.01), text="a"),
            VectorItem(id="m-batch-B", bank_id="b1", vector=_vec_at(1.0, 0.02), text="b"),
        ])

        links = await compute_semantic_links(
            bank_id="b1",
            new_memory_ids=["m-batch-A", "m-batch-B"],
            new_embeddings=[_vec_at(1.0, 0.01), _vec_at(1.0, 0.02)],
            vector_store=vs,
            top_k=5,
            similarity_threshold=0.5,
        )

        # Neither batch member should link to the other.
        for link in links:
            assert not (
                link.source_memory_id.startswith("m-batch-")
                and link.target_memory_id.startswith("m-batch-")
            ), f"same-batch link leaked: {link.source_memory_id}->{link.target_memory_id}"

    @pytest.mark.asyncio
    async def test_below_threshold_dropped(self):
        """Hits below ``similarity_threshold`` are filtered."""
        vs = InMemoryVectorStore()
        # One close, one far.
        await vs.store_vectors([
            VectorItem(id="close", bank_id="b1", vector=_vec_at(1.0, 0.0), text="x"),
            VectorItem(id="far", bank_id="b1", vector=_vec_at(0.0, 1.0), text="y"),
        ])

        links = await compute_semantic_links(
            bank_id="b1",
            new_memory_ids=["new"],
            new_embeddings=[_vec_at(1.0, 0.0)],
            vector_store=vs,
            top_k=5,
            similarity_threshold=0.9,  # high threshold
        )

        target_ids = {link.target_memory_id for link in links}
        assert "close" in target_ids
        assert "far" not in target_ids

    @pytest.mark.asyncio
    async def test_top_k_caps_neighbors(self):
        """``top_k`` is a hard cap on edges per source memory."""
        vs = InMemoryVectorStore()
        await vs.store_vectors([
            VectorItem(
                id=f"existing-{i}",
                bank_id="b1",
                vector=_vec_at(1.0, float(i) * 0.001),
                text=f"x{i}",
            )
            for i in range(20)
        ])

        links = await compute_semantic_links(
            bank_id="b1",
            new_memory_ids=["new"],
            new_embeddings=[_vec_at(1.0, 0.0)],
            vector_store=vs,
            top_k=3,
            similarity_threshold=0.0,
        )

        assert len(links) == 3

    @pytest.mark.asyncio
    async def test_empty_inputs_return_empty(self):
        vs = InMemoryVectorStore()
        assert await compute_semantic_links(
            bank_id="b1", new_memory_ids=[], new_embeddings=[],
            vector_store=vs,
        ) == []

    @pytest.mark.asyncio
    async def test_mismatched_lengths_returns_empty(self):
        """Defensive: ids/embeddings must align index-for-index."""
        vs = InMemoryVectorStore()
        assert await compute_semantic_links(
            bank_id="b1",
            new_memory_ids=["a", "b"],
            new_embeddings=[_vec_at(1.0)],  # length 1
            vector_store=vs,
        ) == []
