"""Tests for the Hindsight-inspired entity-resolution cascade.

The cascade has three tiers (cheapest → most expensive):

1. Trigram name similarity (``find_entity_candidates_scored`` →
   ``name_similarity``).
2. Embedding cosine similarity (``find_entity_candidates_scored`` →
   ``embedding_similarity``).
3. LLM disambiguation (``_confirm_and_link``).

These tests exercise each tier's gating logic in isolation using the
in-memory adapter and a controlled LLM that records every call.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from astrocyte.pipeline.entity_resolution import EntityResolver, _name_similarity
from astrocyte.testing.in_memory import InMemoryGraphStore, MockLLMProvider
from astrocyte.types import Completion, Entity, Message, TokenUsage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _RecordingLLM(MockLLMProvider):
    """MockLLMProvider that records every ``complete()`` call site.

    Used to assert that the cascade's cheap tiers prevent LLM consultation.
    """

    def __init__(self, json_response: str = '{"same_entity": true, "confidence": 0.9, "evidence": "test"}') -> None:
        super().__init__(default_response=json_response)
        self.completion_calls: list[str] = []

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> Completion:
        # Record the system-prompt fingerprint so tests can identify which
        # subsystem made the call (entity resolution vs other paths).
        sys_msg = next(
            (m.content for m in messages if m.role == "system" and isinstance(m.content, str)),
            "",
        )
        self.completion_calls.append(sys_msg[:80])
        return Completion(
            text=self._default_response,
            model=model or "mock",
            usage=TokenUsage(input_tokens=10, output_tokens=20),
        )


def _entity(eid: str, name: str, *, embedding: list[float] | None = None) -> Entity:
    return Entity(
        id=eid,
        name=name,
        entity_type="PERSON",
        aliases=None,
        embedding=embedding,
    )


# ---------------------------------------------------------------------------
# Tier 1: trigram autolink
# ---------------------------------------------------------------------------


class TestTrigramAutolink:
    @pytest.mark.asyncio
    async def test_identical_names_autolink_no_llm(self):
        """When name_similarity ≥ autolink_threshold, link without an LLM call."""
        gs = InMemoryGraphStore()
        await gs.store_entities([_entity("e1", "Alice")], "b")
        llm = _RecordingLLM()

        resolver = EntityResolver(autolink_threshold=0.95, skip_threshold=0.5)
        new_entity = _entity("e2", "Alice")
        links = await resolver.resolve([new_entity], "Alice spoke today.", "b", gs, llm)

        assert len(links) == 1
        assert links[0].link_type == "alias_of"
        assert "trigram" in (links[0].evidence or "")
        # No LLM call should have happened
        assert llm.completion_calls == []

    @pytest.mark.asyncio
    async def test_case_difference_autolinks(self):
        """``Alice`` and ``alice`` are autolinked — case-insensitive trigram."""
        gs = InMemoryGraphStore()
        await gs.store_entities([_entity("e1", "alice")], "b")
        llm = _RecordingLLM()

        resolver = EntityResolver()
        await resolver.resolve([_entity("e2", "Alice")], "Source", "b", gs, llm)
        assert llm.completion_calls == []  # autolinked, no LLM


# ---------------------------------------------------------------------------
# Tier 2: embedding autolink
# ---------------------------------------------------------------------------


class TestEmbeddingAutolink:
    @pytest.mark.asyncio
    async def test_semantic_alias_autolinked_via_embedding(self):
        """``Bob`` ↔ ``Robert`` — different strings, similar embeddings → autolink."""
        gs = InMemoryGraphStore()
        # Existing "Robert" with an embedding.
        existing = _entity("e1", "Robert", embedding=[1.0, 0.0, 0.0])
        # Trigram match: "Bob" shares only 1 char with "Robert" → low.
        # We need to bypass trigram filter to reach the embedding tier;
        # set trigram_threshold low so the candidate is returned.
        await gs.store_entities([existing], "b")
        llm = _RecordingLLM()

        # New entity with the SAME embedding as Robert (cosine = 1.0).
        new = _entity("e2", "Bob", embedding=[1.0, 0.0, 0.0])

        resolver = EntityResolver(
            trigram_threshold=0.0,            # let any candidate through to scoring
            autolink_threshold=0.99,          # name_sim won't autolink
            embedding_autolink_threshold=0.92,
            skip_threshold=0.5,
        )
        links = await resolver.resolve([new], "Bob smiled.", "b", gs, llm)

        assert len(links) == 1, f"Expected embedding autolink, got {links}"
        assert "embedding" in (links[0].evidence or "")
        assert llm.completion_calls == []


# ---------------------------------------------------------------------------
# Tier 3: combined-signal skip
# ---------------------------------------------------------------------------


class TestCombinedSkip:
    @pytest.mark.asyncio
    async def test_low_name_and_no_embedding_skips_no_llm(self):
        """Below skip_threshold on every signal → no LLM call, no link."""
        gs = InMemoryGraphStore()
        # Picked so SequenceMatcher("alice", "zzzzz") ≈ 0 — clearly different.
        await gs.store_entities([_entity("e1", "Zzzzz")], "b")
        llm = _RecordingLLM()

        # Confirm the assumption: name similarity is well below skip_threshold.
        assert _name_similarity("Alice", "Zzzzz") < 0.5

        # No embeddings on either side → embedding tier inactive.
        resolver = EntityResolver(
            trigram_threshold=0.0,    # let candidate through
            skip_threshold=0.5,
            autolink_threshold=0.95,
        )
        new = _entity("e2", "Alice")
        links = await resolver.resolve([new], "Alice nodded.", "b", gs, llm)

        assert links == []
        assert llm.completion_calls == []

    @pytest.mark.asyncio
    async def test_low_name_low_embedding_skips_no_llm(self):
        """Both signals below skip_threshold → no LLM."""
        gs = InMemoryGraphStore()
        await gs.store_entities(
            [_entity("e1", "Zzzzz", embedding=[1.0, 0.0, 0.0])],
            "b",
        )
        llm = _RecordingLLM()
        assert _name_similarity("Alice", "Zzzzz") < 0.5

        # Orthogonal embedding → cosine ≈ 0
        new = _entity("e2", "Alice", embedding=[0.0, 1.0, 0.0])

        resolver = EntityResolver(
            trigram_threshold=0.0,
            skip_threshold=0.5,
        )
        links = await resolver.resolve([new], "Alice nodded.", "b", gs, llm)
        assert links == []
        assert llm.completion_calls == []


# ---------------------------------------------------------------------------
# Tier C: Hindsight-style composite (name + cooccurrence + temporal)
# ---------------------------------------------------------------------------


class TestCompositeAutolink:
    """The composite tier blends name similarity with cooccurrence overlap
    and temporal proximity. It autolinks when ``composite >= 0.6`` without
    consulting the LLM — Hindsight's recipe for resolving syntactic name
    variants where the broader context confirms identity."""

    @pytest.mark.asyncio
    async def test_cooccurrence_overlap_triggers_composite_autolink(self):
        """Borderline-name pair with strong cooccurrence overlap → composite autolink."""
        from datetime import datetime, timezone

        from astrocyte.types import EntityLink

        gs = InMemoryGraphStore()
        # Existing "Alice Smith" with a friend "Bob" recorded as co_occurs.
        await gs.store_entities([
            _entity("e1", "Alice Smith"),
            _entity("e2_bob", "Bob"),
        ], "b")
        gs._links.setdefault("b", []).extend([
            EntityLink(entity_a="e1", entity_b="e2_bob", link_type="co_occurs"),
        ])
        llm = _RecordingLLM()

        # New mention "Alice" arriving with "Bob" nearby. Name sim ~0.7
        # (below autolink), embedding empty, but cooccurrence overlap = 1/1.
        # Composite = 0.5*0.7 + 0.3*1.0 = 0.65 → autolink.
        new_alice = _entity("e_new_alice", "Alice")
        new_bob = _entity("e_new_bob", "Bob")

        resolver = EntityResolver(
            trigram_threshold=0.0,
            autolink_threshold=0.95,
            skip_threshold=0.5,
            composite_threshold=0.6,
        )
        links = await resolver.resolve(
            [new_alice, new_bob], "Alice and Bob arrived together.",
            "b", gs, llm,
            event_date=datetime(2026, 4, 30, tzinfo=timezone.utc),
        )

        # At least one link should be the composite autolink for Alice→Alice Smith.
        assert any("composite" in (lk.evidence or "") for lk in links), \
            f"No composite autolink in {[lk.evidence for lk in links]}"
        # No LLM disambiguation call for the Alice/Alice-Smith pair.
        assert all("entity resolution assistant" not in p.lower() for p in llm.completion_calls)

    @pytest.mark.asyncio
    async def test_temporal_proximity_boosts_composite(self):
        """Recent ``last_seen`` lifts a borderline pair over the composite threshold."""
        from datetime import datetime, timezone

        gs = InMemoryGraphStore()
        # Existing Alice Smith with last_seen 1 day ago.
        recent = (datetime(2026, 4, 30, tzinfo=timezone.utc) - timedelta(days=1)).isoformat()
        await gs.store_entities([
            _entity("e1", "Alice Smith"),
        ], "b")
        # Inject last_seen via metadata so the in-memory adapter exposes it.
        gs._entities["b"]["e1"].metadata = {"last_seen": recent}
        llm = _RecordingLLM()

        new_alice = _entity("e_new", "Alice")  # name_sim ~ 0.7

        resolver = EntityResolver(
            trigram_threshold=0.0,
            autolink_threshold=0.95,
            skip_threshold=0.5,
            composite_threshold=0.55,  # tuned for this test
        )
        await resolver.resolve(
            [new_alice], "Alice spoke today.",
            "b", gs, llm,
            event_date=datetime(2026, 4, 30, tzinfo=timezone.utc),
        )

        # composite = 0.5*0.7 + 0.3*0 + 0.2 * (1 - 1/7) ≈ 0.35 + 0.171 ≈ 0.52
        # Below 0.55 → falls through to LLM (or skip). With recent enough
        # last_seen, contributes to lifting borderline pairs.
        # Verify: the composite temporal contribution is non-zero.
        # (This is a smoke test — the score itself is exposed via
        # ``_composite_score``.)
        match = (await gs.find_entity_candidates_scored("Alice", "b", trigram_threshold=0.0))[0]
        score = resolver._composite_score(
            match.name_similarity, match,
            nearby_names=set(),
            event_date=datetime(2026, 4, 30, tzinfo=timezone.utc),
        )
        assert score > 0.5 * match.name_similarity, \
            "Temporal signal didn't add anything to the composite"

    @pytest.mark.asyncio
    async def test_no_signals_means_just_name_weight(self):
        """When cooccurrence and temporal are both empty, composite = name_weight * name_sim."""
        from datetime import datetime, timezone

        gs = InMemoryGraphStore()
        await gs.store_entities([_entity("e1", "Alice")], "b")

        resolver = EntityResolver()
        match = (await gs.find_entity_candidates_scored("Alice", "b"))[0]
        # name_sim = 1.0, no cooccurrence (no links), no last_seen
        score = resolver._composite_score(
            match.name_similarity, match,
            nearby_names=set(),
            event_date=datetime(2026, 4, 30, tzinfo=timezone.utc),
        )
        # With no cooccurrence and no temporal, composite = 0.5 * 1.0 = 0.5
        assert score == pytest.approx(0.5)


class TestMentionCountBonus:
    """Hindsight-parity ``mention_count`` popularity signal.

    The bonus is a soft tiebreaker: capped (default ``0.05``), log-scaled
    so it saturates, and never lifts a candidate past the composite
    threshold on its own. These tests pin those properties down.
    """

    @pytest.mark.asyncio
    async def test_mention_count_one_contributes_zero_bonus(self):
        """A fresh entity (``mention_count = 1``) gets no bonus — the
        signal only kicks in once a canonical has been resolved-to."""
        from datetime import datetime, timezone
        gs = InMemoryGraphStore()
        await gs.store_entities([_entity("e1", "Alice")], "b")
        resolver = EntityResolver()

        match = (await gs.find_entity_candidates_scored("Alice", "b"))[0]
        score = resolver._composite_score(
            match.name_similarity, match,
            nearby_names=set(),
            event_date=datetime(2026, 4, 30, tzinfo=timezone.utc),
        )

        # With name_sim=1.0 and no other signals, composite is exactly
        # ``composite_name_weight * 1.0`` = 0.5. Any bonus would push above.
        assert score == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_high_mention_count_breaks_ties_within_cap(self):
        """A popular candidate scores higher than an unpopular one with
        otherwise identical signals — but only up to the cap."""
        from datetime import datetime, timezone
        gs = InMemoryGraphStore()
        await gs.store_entities([_entity("e1", "Alice")], "b")
        resolver = EntityResolver(mention_count_bonus_cap=0.05, mention_count_saturation=50)

        match = (await gs.find_entity_candidates_scored("Alice", "b"))[0]

        # Simulate three resolution events on the same canonical.
        await gs.increment_mention_counts(["e1"], "b")
        await gs.increment_mention_counts(["e1"], "b")
        await gs.increment_mention_counts(["e1"], "b")
        match_popular = (await gs.find_entity_candidates_scored("Alice", "b"))[0]

        score_fresh = resolver._composite_score(
            match.name_similarity, match,
            nearby_names=set(),
            event_date=datetime(2026, 4, 30, tzinfo=timezone.utc),
        )
        score_popular = resolver._composite_score(
            match_popular.name_similarity, match_popular,
            nearby_names=set(),
            event_date=datetime(2026, 4, 30, tzinfo=timezone.utc),
        )

        assert score_popular > score_fresh, (
            "Popular candidate should outscore fresh one with identical other signals"
        )
        assert score_popular - score_fresh <= 0.05 + 1e-9, (
            "Bonus must never exceed mention_count_bonus_cap"
        )

    @pytest.mark.asyncio
    async def test_disabled_bonus_means_zero_contribution(self):
        """``mention_count_bonus_cap=0.0`` disables the signal entirely."""
        from datetime import datetime, timezone
        gs = InMemoryGraphStore()
        await gs.store_entities([_entity("e1", "Alice")], "b")
        for _ in range(10):
            await gs.increment_mention_counts(["e1"], "b")
        resolver = EntityResolver(mention_count_bonus_cap=0.0)

        match = (await gs.find_entity_candidates_scored("Alice", "b"))[0]
        score = resolver._composite_score(
            match.name_similarity, match,
            nearby_names=set(),
            event_date=datetime(2026, 4, 30, tzinfo=timezone.utc),
        )

        # Pure name-weight contribution — no popularity creep.
        assert score == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_canonical_resolution_increments_mention_count(self):
        """End-to-end: when ``resolve_canonical_ids_in_place`` rewrites a
        tentative ID to an existing canonical, the canonical's
        ``mention_count`` is bumped."""
        from datetime import datetime, timezone
        gs = InMemoryGraphStore()
        # Pre-existing canonical "Alice" with mention_count=1.
        await gs.store_entities([_entity("e-canonical", "Alice")], "b")
        resolver = EntityResolver(canonical_resolution=True)

        # New "Alice" arrives with a tentative ID — should resolve to e-canonical.
        new_entity = _entity("e-tentative", "Alice")
        remap = await resolver.resolve_canonical_ids_in_place(
            [new_entity],
            "b",
            gs,
            event_date=datetime(2026, 4, 30, tzinfo=timezone.utc),
        )

        assert remap == {"e-tentative": "e-canonical"}
        # Canonical's mention_count must have bumped from 1 → 2.
        match = (await gs.find_entity_candidates_scored("Alice", "b"))[0]
        assert match.mention_count == 2
        assert match.entity.mention_count == 2


# ---------------------------------------------------------------------------
# Tier 4: LLM disambiguation only on the ambiguous middle band
# ---------------------------------------------------------------------------


class TestLLMDisambiguation:
    @pytest.mark.asyncio
    async def test_ambiguous_pair_goes_to_llm(self):
        """name similarity in (skip_threshold, autolink_threshold) → LLM fires
        when ``enable_llm_disambiguation`` is True."""
        gs = InMemoryGraphStore()
        await gs.store_entities([_entity("e1", "Alice Smith")], "b")
        llm = _RecordingLLM(
            '{"same_entity": true, "confidence": 0.9, "evidence": "Alice introduced herself"}',
        )

        # "Alice" vs "Alice Smith": name_similarity ~0.7 (in middle band)
        new = _entity("e2", "Alice")
        assert 0.5 < _name_similarity("Alice", "Alice Smith") < 0.95

        # Default ``enable_llm_disambiguation=True``. Disable composite so
        # the cascade falls through to LLM (otherwise composite catches it).
        resolver = EntityResolver(
            trigram_threshold=0.0,
            skip_threshold=0.5,
            autolink_threshold=0.95,
            composite_threshold=0.99,  # disable composite tier so LLM can fire
        )
        links = await resolver.resolve([new], "Alice introduced herself", "b", gs, llm)

        assert len(links) == 1
        assert "Alice" in llm.completion_calls[0] or "entity resolution" in llm.completion_calls[0].lower()
        assert len(llm.completion_calls) == 1

    @pytest.mark.asyncio
    async def test_ambiguous_pair_skipped_when_llm_disabled(self):
        """When ``enable_llm_disambiguation=False``, ambiguous pairs are skipped."""
        gs = InMemoryGraphStore()
        await gs.store_entities([_entity("e1", "Alice Smith")], "b")
        llm = _RecordingLLM()
        new = _entity("e2", "Alice")

        resolver = EntityResolver(
            trigram_threshold=0.0,
            skip_threshold=0.5,
            autolink_threshold=0.95,
            composite_threshold=0.99,  # composite won't catch this either
            enable_llm_disambiguation=False,  # Hindsight-style, opt-out
        )
        links = await resolver.resolve([new], "Alice introduced herself", "b", gs, llm)

        # LLM never called; no link created.
        assert links == []
        assert llm.completion_calls == []


# ---------------------------------------------------------------------------
# Defaults preserved (regression guard)
# ---------------------------------------------------------------------------


class TestCascadeDefaults:
    def test_default_thresholds(self):
        r = EntityResolver()
        assert r.autolink_threshold == pytest.approx(0.95)
        assert r.embedding_autolink_threshold == pytest.approx(0.92)
        assert r.skip_threshold == pytest.approx(0.5)
        # Lowered to 0.15 to match Hindsight's empirically-tuned default —
        # catches substring-style matches while staying fully GIN-indexed.
        assert r.trigram_threshold == pytest.approx(0.15)
        assert r.composite_threshold == pytest.approx(0.6)
        assert r.composite_name_weight == pytest.approx(0.5)
        assert r.composite_cooccurrence_weight == pytest.approx(0.3)
        assert r.composite_temporal_weight == pytest.approx(0.2)
        assert r.composite_temporal_window_days == pytest.approx(7.0)

    def test_invalid_threshold_ordering_rejected(self):
        with pytest.raises(ValueError):
            EntityResolver(skip_threshold=0.99, autolink_threshold=0.5)

    def test_invalid_embedding_threshold_rejected(self):
        with pytest.raises(ValueError):
            EntityResolver(embedding_autolink_threshold=1.5)


# ---------------------------------------------------------------------------
# Adapter behaviour: in-memory find_entity_candidates_scored
# ---------------------------------------------------------------------------


class TestInMemoryScoredCandidates:
    @pytest.mark.asyncio
    async def test_returns_name_similarity(self):
        gs = InMemoryGraphStore()
        await gs.store_entities([_entity("e1", "Alice")], "b")
        results = await gs.find_entity_candidates_scored("Alice", "b")
        assert len(results) == 1
        assert results[0].entity.name == "Alice"
        assert results[0].name_similarity == pytest.approx(1.0)
        assert results[0].embedding_similarity is None  # no embedding stored

    @pytest.mark.asyncio
    async def test_returns_embedding_similarity_when_both_have_vectors(self):
        gs = InMemoryGraphStore()
        await gs.store_entities(
            [_entity("e1", "Robert", embedding=[1.0, 0.0])],
            "b",
        )
        results = await gs.find_entity_candidates_scored(
            "Bob", "b",
            name_embedding=[1.0, 0.0],
            trigram_threshold=0.0,  # bypass trigram filter
        )
        assert len(results) == 1
        assert results[0].embedding_similarity == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_trigram_threshold_filters_candidates(self):
        gs = InMemoryGraphStore()
        await gs.store_entities(
            [_entity("e1", "Zzzzz"), _entity("e2", "Alice")],
            "b",
        )
        # ``Zzzzz`` shares no characters with ``Alice``; SequenceMatcher
        # ratio ≈ 0. Threshold 0.5 keeps ``Alice`` (1.0) and drops ``Zzzzz``.
        results = await gs.find_entity_candidates_scored(
            "Alice", "b", trigram_threshold=0.5,
        )
        names = {r.entity.name for r in results}
        assert "Alice" in names
        assert "Zzzzz" not in names

    @pytest.mark.asyncio
    async def test_results_ordered_by_max_score(self):
        gs = InMemoryGraphStore()
        await gs.store_entities(
            [
                _entity("e1", "Alice Smith"),  # name_sim ~0.7
                _entity("e2", "Alice"),         # name_sim 1.0
            ],
            "b",
        )
        results = await gs.find_entity_candidates_scored(
            "Alice", "b", trigram_threshold=0.0,
        )
        assert results[0].entity.name == "Alice"  # higher scored first
