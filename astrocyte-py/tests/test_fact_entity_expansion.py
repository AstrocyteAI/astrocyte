"""M12.4: entity-graph expansion unit tests."""

from __future__ import annotations

from astrocyte.pipeline.fact_entity_expansion import expand_via_entity_graph
from astrocyte.types import PageIndexFactHit


class FakeStore:
    """Stub PageIndexStore exposing only ``search_facts_by_entity``.

    Hits are keyed by entity name (case-sensitive) so tests can pin the
    expansion fan-out per seed entity. ``search_facts_by_entity`` returns
    the configured list verbatim, respecting ``top_k`` truncation.
    """

    def __init__(
        self,
        hits_by_entity: dict[str, list[PageIndexFactHit]] | None = None,
        *,
        raise_for: set[str] | None = None,
    ) -> None:
        self.hits_by_entity = hits_by_entity or {}
        self.raise_for = raise_for or set()
        self.calls: list[tuple[str, str | None]] = []

    async def search_facts_by_entity(
        self,
        bank_id: str,
        entity_name: str,
        *,
        top_k: int = 50,
        document_id: str | None = None,
    ) -> list[PageIndexFactHit]:
        self.calls.append((entity_name, document_id))
        if entity_name in self.raise_for:
            raise RuntimeError(f"db error for entity {entity_name!r}")
        return self.hits_by_entity.get(entity_name, [])[:top_k]


def _hit(
    fact_id: str,
    entities: list[str],
    *,
    line_num: int = 1,
    text: str = "fact text",
    score: float = 0.5,
) -> PageIndexFactHit:
    return PageIndexFactHit(
        fact_id=fact_id,
        document_id="doc-1",
        line_num=line_num,
        text=text,
        fact_type="experience",
        speaker="user",
        occurred_start=None,
        occurred_end=None,
        entities=list(entities),
        score=score,
    )


async def test_empty_initial_hits_returns_empty() -> None:
    out = await expand_via_entity_graph(
        [],
        store=FakeStore(),
        bank_id="b1",
    )
    assert out == []


async def test_no_entities_on_seed_hits_returns_empty() -> None:
    seeds = [_hit("s1", entities=[])]
    out = await expand_via_entity_graph(
        seeds,
        store=FakeStore(),
        bank_id="b1",
    )
    assert out == []


async def test_happy_path_expands_via_entity() -> None:
    # Seed mentions Dr. Patel; the store knows two other facts that
    # also mention Dr. Patel. Both should be returned.
    seeds = [_hit("s1", entities=["Dr. Patel", "role:doctor"])]
    neighbor_a = _hit("n1", entities=["Dr. Patel", "Sunnyvale Clinic"])
    neighbor_b = _hit("n2", entities=["Dr. Patel"], line_num=42)
    store = FakeStore(
        hits_by_entity={
            "Dr. Patel": [neighbor_a, neighbor_b],
            "role:doctor": [],
        }
    )
    out = await expand_via_entity_graph(
        seeds,
        store=store,
        bank_id="b1",
    )
    assert {h.fact_id for h in out} == {"n1", "n2"}


async def test_dedupes_against_initial_hits() -> None:
    # The store happens to return the seed itself as a "neighbor".
    # Should NOT appear in the expanded set.
    seed = _hit("s1", entities=["Dr. Patel"])
    store = FakeStore(
        hits_by_entity={
            "Dr. Patel": [seed, _hit("n1", entities=["Dr. Patel"])],
        }
    )
    out = await expand_via_entity_graph(
        [seed],
        store=store,
        bank_id="b1",
    )
    assert [h.fact_id for h in out] == ["n1"]


async def test_dedupes_across_entities() -> None:
    # The same neighbor fact mentions two seed entities; it appears
    # in both entity's neighbor lists. Should appear ONCE in the output.
    seed = _hit("s1", entities=["Dr. Patel", "role:doctor"])
    shared = _hit("n1", entities=["Dr. Patel", "role:doctor"])
    store = FakeStore(
        hits_by_entity={
            "Dr. Patel": [shared],
            "role:doctor": [shared],
        }
    )
    out = await expand_via_entity_graph(
        [seed],
        store=store,
        bank_id="b1",
    )
    assert [h.fact_id for h in out] == ["n1"]


async def test_max_seed_hits_caps_seed_pool() -> None:
    # Only first 2 seed hits should contribute entities.
    seeds = [
        _hit("s1", entities=["A"]),
        _hit("s2", entities=["B"]),
        _hit("s3", entities=["C"]),  # ignored — beyond max_seed_hits
    ]
    store = FakeStore(
        hits_by_entity={
            "A": [_hit("nA", entities=["A"])],
            "B": [_hit("nB", entities=["B"])],
            "C": [_hit("nC", entities=["C"])],
        }
    )
    out = await expand_via_entity_graph(
        seeds,
        store=store,
        bank_id="b1",
        max_seed_hits=2,
    )
    assert {h.fact_id for h in out} == {"nA", "nB"}


async def test_max_seed_entities_caps_distinct_entities() -> None:
    seeds = [
        _hit("s1", entities=["A", "B", "C", "D", "E"]),
    ]
    store = FakeStore(hits_by_entity={e: [_hit(f"n{e}", entities=[e])] for e in "ABCDE"})
    out = await expand_via_entity_graph(
        seeds,
        store=store,
        bank_id="b1",
        max_seed_entities=3,
    )
    # Only A, B, C are used as seeds → only nA, nB, nC come back
    assert {h.fact_id for h in out} == {"nA", "nB", "nC"}


async def test_max_expanded_facts_caps_total_output() -> None:
    seed = _hit("s1", entities=["Dr. Patel"])
    store = FakeStore(
        hits_by_entity={
            "Dr. Patel": [_hit(f"n{i}", entities=["Dr. Patel"]) for i in range(50)],
        }
    )
    out = await expand_via_entity_graph(
        [seed],
        store=store,
        bank_id="b1",
        max_expanded_facts=5,
    )
    assert len(out) == 5


async def test_max_neighbor_facts_per_entity_caps_per_entity_fan_out() -> None:
    seed = _hit("s1", entities=["Dr. Patel"])
    store = FakeStore(
        hits_by_entity={
            "Dr. Patel": [_hit(f"n{i}", entities=["Dr. Patel"]) for i in range(50)],
        }
    )
    out = await expand_via_entity_graph(
        [seed],
        store=store,
        bank_id="b1",
        max_neighbor_facts_per_entity=3,
        max_expanded_facts=100,
    )
    assert len(out) == 3


async def test_case_insensitive_seed_entity_dedup() -> None:
    # "Dr. Patel" / "dr. patel" / "DR. PATEL" are all the same seed.
    seeds = [_hit("s1", entities=["Dr. Patel", "dr. patel", "DR. PATEL"])]
    store = FakeStore(
        hits_by_entity={
            "Dr. Patel": [_hit("n1", entities=["Dr. Patel"])],
        }
    )
    out = await expand_via_entity_graph(
        seeds,
        store=store,
        bank_id="b1",
    )
    # Only one search call for the deduped entity
    assert len(store.calls) == 1
    assert store.calls[0][0] == "Dr. Patel"
    assert [h.fact_id for h in out] == ["n1"]


async def test_per_entity_store_failure_isolated() -> None:
    # Failure on one entity's lookup must not abort the expansion.
    seed = _hit("s1", entities=["A", "B"])
    store = FakeStore(
        hits_by_entity={
            "A": [_hit("nA", entities=["A"])],
            "B": [_hit("nB", entities=["B"])],
        },
        raise_for={"A"},
    )
    out = await expand_via_entity_graph(
        [seed],
        store=store,
        bank_id="b1",
    )
    assert [h.fact_id for h in out] == ["nB"]


async def test_document_id_threaded_through() -> None:
    seed = _hit("s1", entities=["A"])
    store = FakeStore(
        hits_by_entity={
            "A": [_hit("n1", entities=["A"])],
        }
    )
    await expand_via_entity_graph(
        [seed],
        store=store,
        bank_id="b1",
        document_id="doc-42",
    )
    assert store.calls == [("A", "doc-42")]
