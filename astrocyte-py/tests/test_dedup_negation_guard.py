"""Dedup must not treat a statement and its negation as duplicates.

The measurement behind this (bge-small-en-v1.5, the self-eval embedder):

    "The user is allergic to peanuts."  vs  "...is not allergic..."   0.920
    "I prefer window seats on flights." vs  "I never want window..."  0.842
    "We decided to use Postgres..."     vs  "...not to use Postgres"  0.827
    unrelated control                                                 0.396

A reversal embeds as a near-duplicate. ``DedupDetector`` decided on cosine
alone, and the default retain action *drops* the duplicate chunk — so a
correction could be silently discarded as a repeat of the fact it corrects.
At the 0.95 default this was not firing with bge-small (worst pair 0.920, a
0.03 margin that had never been measured), and it was unmeasured for
text-embedding-3-small, the AML submission embedder.

Idea from engrim (timgordontg/engrim): "similarity cannot establish the scope
of a negation". Adapted: engrim requires exact equality whenever either side
contains a negator, which suits short curated records but would disable dedup
for our long conversation chunks (nearly every one contains a "not"). We veto
only when the token *difference* contains a negator.
"""

from __future__ import annotations

import re

import pytest

from astrocyte.policy.signal_quality import DedupDetector, differs_by_negation
from astrocyte.testing.in_memory import MockLLMProvider

V = [0.6, 0.8, 0.0]  # any fixed vector; identical embeddings = cosine 1.0

ALLERGIC = "The user is allergic to peanuts."
NOT_ALLERGIC = "The user is not allergic to peanuts."


class TestDetector:
    def test_reversal_is_not_a_duplicate_even_at_cosine_1(self):
        d = DedupDetector(similarity_threshold=0.95)
        d.add("b", "m1", V, text=ALLERGIC)
        is_dup, sim = d.is_duplicate("b", V, text=NOT_ALLERGIC)
        assert sim == pytest.approx(1.0)
        assert is_dup is False, "a negated statement was dedup'd against the original"
        assert d.negation_overrides == 1

    def test_true_duplicate_still_dedups(self):
        """The guard must not break dedup itself."""
        d = DedupDetector(similarity_threshold=0.95)
        d.add("b", "m1", V, text=ALLERGIC)
        assert d.is_duplicate("b", V, text=ALLERGIC)[0] is True
        assert d.negation_overrides == 0

    def test_negated_true_duplicate_still_dedups(self):
        """Both sides negated identically is a duplicate, not a reversal."""
        d = DedupDetector(similarity_threshold=0.95)
        d.add("b", "m1", V, text=NOT_ALLERGIC)
        assert d.is_duplicate("b", V, text=NOT_ALLERGIC)[0] is True

    def test_long_chunk_reingest_differing_only_in_timestamp_still_dedups(self):
        """The reason for 'differs by' over engrim's 'either side contains':
        a long chunk almost always contains a negator somewhere."""
        a = "**user** [2023-01-01]: we will not use mongo, we use postgres"
        b = "**user** [2023-02-09]: we will not use mongo, we use postgres"
        d = DedupDetector(similarity_threshold=0.95)
        d.add("b", "m1", V, text=a)
        assert d.is_duplicate("b", V, text=b)[0] is True

    def test_without_text_behaviour_is_unchanged(self):
        """Callers that pass no text keep cosine-only semantics."""
        d = DedupDetector(similarity_threshold=0.95)
        d.add("b", "m1", V)
        assert d.is_duplicate("b", V)[0] is True

    def test_remove_still_works_with_text_entries(self):
        d = DedupDetector()
        d.add("b", "m1", V, text=ALLERGIC)
        assert d.remove("b", "m1") is True
        assert d.is_duplicate("b", V, text=ALLERGIC)[0] is False


@pytest.mark.parametrize(
    "a,b",
    [
        (ALLERGIC, NOT_ALLERGIC),
        ("Alice will attend in March.", "Alice will no longer attend in March."),
        ("I can come.", "I can't come."),
        ("I can come.", "I can’t come."),  # curly apostrophe
        ("I prefer window seats.", "I never want window seats."),
    ],
)
def test_reversals_are_detected(a, b):
    assert differs_by_negation(a, b) and differs_by_negation(b, a)


def test_antonyms_are_a_documented_gap():
    """Pinned so the limit stays explicit: no negator, no veto."""
    assert differs_by_negation("Enable caching.", "Disable caching.") is False


class _NegationBlindProvider(MockLLMProvider):
    """Embeds with negators removed — the measured failure taken to its limit,
    so a reversal and its original get IDENTICAL vectors."""

    async def embed(self, texts, model=None):
        stripped = [re.sub(r"\b(not|no|never)\b|n't", "", t, flags=re.I) for t in texts]
        return await super().embed(stripped, model=model)


async def test_end_to_end_a_correction_survives_retain():
    from astrocyte._astrocyte import Astrocyte
    from astrocyte.config import AstrocyteConfig
    from astrocyte.pipeline.orchestrator import PipelineOrchestrator
    from astrocyte.testing.in_memory import InMemoryVectorStore

    cfg = AstrocyteConfig()
    cfg.access_control.enabled = False
    brain = Astrocyte(cfg)
    pipeline = PipelineOrchestrator(
        vector_store=InMemoryVectorStore(),
        llm_provider=_NegationBlindProvider(),
        enable_observation_consolidation=False,
    )
    brain.set_pipeline(pipeline)

    first = await brain.retain(ALLERGIC, bank_id="b1")
    correction = await brain.retain(NOT_ALLERGIC, bank_id="b1")

    assert first.stored
    assert correction.stored, f"the correction was dropped as a duplicate: {correction}"
    hits = (await brain.recall("peanut allergy", bank_id="b1", max_results=10)).hits
    texts = " ".join(h.text for h in hits)
    assert "not allergic" in texts, "the correction is unrecallable"
