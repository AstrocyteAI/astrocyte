"""M12.1: fact-grain extraction unit tests."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from astrocyte.pipeline.section_fact_extraction import (
    _parse_iso_date,
    extract_facts_for_section,
)
from astrocyte.testing.in_memory import InMemoryPageIndexStore
from astrocyte.types import (
    Completion,
    PageIndexDocument,
    PageIndexFact,
    PageIndexSection,
)


def _section(line_num: int, summary: str, date_str: str = "2023-05-08") -> PageIndexSection:
    return PageIndexSection(
        document_id="doc-1",
        line_num=line_num,
        node_id=f"{line_num:04d}",
        title=f"node-{line_num}",
        summary=summary,
        session_date=datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc),
    )


class TestParseIsoDate:
    def test_date_only(self) -> None:
        assert _parse_iso_date("2023-05-07") == datetime(2023, 5, 7)

    def test_full_iso_with_z(self) -> None:
        out = _parse_iso_date("2023-05-07T12:00:00Z")
        assert out == datetime(2023, 5, 7, 12, 0, tzinfo=timezone.utc)

    def test_none(self) -> None:
        assert _parse_iso_date(None) is None

    def test_garbage(self) -> None:
        assert _parse_iso_date("not a date") is None


class TestExtractFactsForSection:
    @pytest.fixture
    def section(self) -> PageIndexSection:
        return _section(5, "User discussed health.")

    async def test_happy_path_three_facts(self, section: PageIndexSection) -> None:
        provider = MagicMock()
        provider.complete = AsyncMock(
            return_value=Completion(
                text='{"facts": ['
                '{"text": "User visited Dr. Patel on May 7, 2023.",'
                ' "fact_type": "experience", "speaker": "user",'
                ' "occurred_start": "2023-05-07", "occurred_end": null,'
                ' "entities": ["Dr. Patel", "role:doctor"]},'
                '{"text": "User has been seeing Dr. Patel for 6 months.",'
                ' "fact_type": "experience", "speaker": "user",'
                ' "occurred_start": null, "occurred_end": null,'
                ' "entities": ["Dr. Patel", "role:doctor"]},'
                '{"text": "User prefers Dr. Patel over previous clinic.",'
                ' "fact_type": "preference", "speaker": "user",'
                ' "occurred_start": null, "occurred_end": null,'
                ' "entities": ["Dr. Patel", "role:doctor"]}'
                "]}",
                model="gpt-4o-mini",
            )
        )
        facts = await extract_facts_for_section(
            provider,
            section,
            "some section text",
            bank_id="b1",
        )
        assert len(facts) == 3
        assert facts[0].fact_type == "experience"
        assert facts[0].occurred_start == datetime(2023, 5, 7)
        assert facts[2].fact_type == "preference"
        assert all(f.bank_id == "b1" for f in facts)
        assert all(f.document_id == "doc-1" and f.line_num == 5 for f in facts)
        assert all(isinstance(f.id, str) and len(f.id) > 10 for f in facts)

    async def test_empty_facts_list_returns_empty(self, section) -> None:
        provider = MagicMock()
        provider.complete = AsyncMock(
            return_value=Completion(
                text='{"facts": []}',
                model="gpt-4o-mini",
            )
        )
        facts = await extract_facts_for_section(
            provider,
            section,
            "chit-chat",
            bank_id="b1",
        )
        assert facts == []

    async def test_assistant_utterance_extracted_as_experience(self, section) -> None:
        """M25 (Hindsight-parity): assistant utterances are extracted as
        ``fact_type='experience'`` with ``speaker='assistant'``, NOT as
        a separate ``assistant_statement`` type.

        Replaces the pre-M25 ``assistant_statement`` fact_type. The
        speaker field carries the perspective signal; fact_type uses
        Hindsight's binary world/experience taxonomy. See
        ``section_fact_extraction.py`` `_VALID_FACT_TYPES` docstring
        for the architecture decision.
        """
        provider = MagicMock()
        provider.complete = AsyncMock(
            return_value=Completion(
                text='{"facts": ['
                '{"text": "Assistant recommended a saline rinse for post-nasal drip.",'
                ' "fact_type": "experience", "speaker": "assistant",'
                ' "occurred_start": null, "occurred_end": null,'
                ' "entities": ["saline rinse", "post-nasal drip"]}'
                "]}",
                model="gpt-4o-mini",
            )
        )
        facts = await extract_facts_for_section(
            provider,
            section,
            "text",
            bank_id="b1",
        )
        assert len(facts) == 1
        assert facts[0].fact_type == "experience"
        assert facts[0].speaker == "assistant"
        assert "saline rinse" in facts[0].entities

    async def test_m27_confidence_score_extracted(self, section) -> None:
        """M27 #2 — extraction prompt asks for ``confidence`` per fact;
        when the LLM emits a valid 0.0-1.0 float it's stored on
        ``MemoryFact.confidence_score`` and ``MemoryFactHit.confidence_score``.
        """
        provider = MagicMock()
        provider.complete = AsyncMock(
            return_value=Completion(
                text='{"facts": ['
                '{"text": "User loves hiking.", "fact_type": "preference",'
                ' "speaker": "user", "entities": ["hiking"], "confidence": 0.95}'
                "]}",
                model="gpt-4o-mini",
            )
        )
        facts = await extract_facts_for_section(provider, section, "text", bank_id="b1")
        assert len(facts) == 1
        assert facts[0].confidence_score == 0.95

    async def test_m27_confidence_score_clamps_out_of_range(self, section) -> None:
        """Out-of-range values are clamped to [0.0, 1.0] rather than dropped."""
        provider = MagicMock()
        provider.complete = AsyncMock(
            return_value=Completion(
                text='{"facts": ['
                '{"text": "f1", "fact_type": "experience", "entities": [], "confidence": 1.5},'
                '{"text": "f2", "fact_type": "experience", "entities": [], "confidence": -0.3}'
                "]}",
                model="gpt-4o-mini",
            )
        )
        facts = await extract_facts_for_section(provider, section, "text", bank_id="b1")
        assert len(facts) == 2
        assert facts[0].confidence_score == 1.0  # clamped from 1.5
        assert facts[1].confidence_score == 0.0  # clamped from -0.3

    async def test_m31b_confidence_score_absent_defaults_to_0_7(self, section) -> None:
        """M31b Fix C — when LLM omits confidence, default to 0.7 (the
        documented M27 default) instead of None. Rationale: Fix 3's
        confidence-aware abstention rule in the answerer prompt needs
        SOMETHING to hedge against; None means the rule never fires.

        Supersedes the original M27 test that asserted None — the
        "distinguish no-signal from low-confidence" framing turned out
        to disable Fix 3 entirely on facts the LLM didn't tag.
        """
        provider = MagicMock()
        provider.complete = AsyncMock(
            return_value=Completion(
                text='{"facts": ['
                '{"text": "no-conf", "fact_type": "experience", "entities": []}'
                "]}",
                model="gpt-4o-mini",
            )
        )
        facts = await extract_facts_for_section(provider, section, "text", bank_id="b1")
        assert facts[0].confidence_score == 0.7

    async def test_m31b_invalid_confidence_value_defaults_to_0_7(self, section) -> None:
        """Malformed value (string, non-numeric, etc.) also defaults to 0.7."""
        provider = MagicMock()
        provider.complete = AsyncMock(
            return_value=Completion(
                text='{"facts": ['
                '{"text": "bad-conf", "fact_type": "experience", '
                '"entities": [], "confidence": "not-a-number"}'
                "]}",
                model="gpt-4o-mini",
            )
        )
        facts = await extract_facts_for_section(provider, section, "text", bank_id="b1")
        assert facts[0].confidence_score == 0.7

    async def test_m27_mentioned_at_populated_from_section(self, section) -> None:
        """M27 #6 — every fact gets ``mentioned_at`` from the section's
        session_date, distinct from any ``occurred_start`` the LLM emits.
        """
        provider = MagicMock()
        provider.complete = AsyncMock(
            return_value=Completion(
                text='{"facts": ['
                '{"text": "user did x", "fact_type": "experience",'
                ' "speaker": "user", "occurred_start": "2023-05-01",'
                ' "entities": []}'
                "]}",
                model="gpt-4o-mini",
            )
        )
        facts = await extract_facts_for_section(provider, section, "text", bank_id="b1")
        assert len(facts) == 1
        # mentioned_at comes from section.session_date (May 8 per fixture)
        assert facts[0].mentioned_at == section.session_date
        # occurred_start is independent — when the event actually happened
        from datetime import datetime
        assert facts[0].occurred_start == datetime(2023, 5, 1)
        # The two timestamps differ: event happened May 1, was discussed May 8
        assert facts[0].mentioned_at != facts[0].occurred_start

    async def test_legacy_assistant_statement_remapped(self, section) -> None:
        """M25 legacy compat: an LLM that still emits the pre-M25
        ``assistant_statement`` tag (e.g. a model trained on the old
        prompt or a legacy ingest replay) has its row remapped to the
        canonical (experience + speaker='assistant') shape instead of
        being dropped on read.
        """
        provider = MagicMock()
        provider.complete = AsyncMock(
            return_value=Completion(
                text='{"facts": ['
                '{"text": "Assistant suggested switching to nasal spray.",'
                ' "fact_type": "assistant_statement",'
                # Note: no explicit "speaker" key — the remap path
                # backfills it to 'assistant'.
                ' "entities": ["nasal spray"]}'
                "]}",
                model="gpt-4o-mini",
            )
        )
        facts = await extract_facts_for_section(
            provider,
            section,
            "text",
            bank_id="b1",
        )
        assert len(facts) == 1
        assert facts[0].fact_type == "experience"
        assert facts[0].speaker == "assistant"

    async def test_mixed_user_and_assistant_facts(self, section) -> None:
        """Multi-output: one section yields rows of multiple fact_types.
        Post-M25 the assistant utterance is fact_type='experience' with
        speaker='assistant', NOT a separate assistant_statement bucket.
        """
        provider = MagicMock()
        provider.complete = AsyncMock(
            return_value=Completion(
                text='{"facts": ['
                '{"text": "User visited Dr. Patel.", "fact_type": "experience",'
                ' "speaker": "user", "entities": ["Dr. Patel"]},'
                '{"text": "User prefers clinic A.", "fact_type": "preference",'
                ' "speaker": "user", "entities": ["clinic A"]},'
                '{"text": "Assistant suggested switching to nasal spray.",'
                ' "fact_type": "experience", "speaker": "assistant",'
                ' "entities": ["nasal spray"]}'
                "]}",
                model="gpt-4o-mini",
            )
        )
        facts = await extract_facts_for_section(
            provider,
            section,
            "text",
            bank_id="b1",
        )
        assert len(facts) == 3
        fact_types = {f.fact_type for f in facts}
        # M25: fact_type collapses to {experience, preference}; speaker
        # carries the user/assistant distinction.
        assert fact_types == {"experience", "preference"}
        speakers = {f.speaker for f in facts}
        assert speakers == {"user", "assistant"}

    async def test_invalid_fact_type_dropped(self, section) -> None:
        provider = MagicMock()
        provider.complete = AsyncMock(
            return_value=Completion(
                text='{"facts": ['
                '{"text": "valid", "fact_type": "experience", "entities": []},'
                '{"text": "invalid type", "fact_type": "gibberish", "entities": []}'
                "]}",
                model="gpt-4o-mini",
            )
        )
        facts = await extract_facts_for_section(
            provider,
            section,
            "text",
            bank_id="b1",
        )
        assert len(facts) == 1
        assert facts[0].fact_type == "experience"

    async def test_missing_text_dropped(self, section) -> None:
        provider = MagicMock()
        provider.complete = AsyncMock(
            return_value=Completion(
                text='{"facts": [{"text": "", "fact_type": "experience"},{"text": "valid", "fact_type": "world"}]}',
                model="gpt-4o-mini",
            )
        )
        facts = await extract_facts_for_section(
            provider,
            section,
            "text",
            bank_id="b1",
        )
        assert len(facts) == 1
        assert facts[0].text == "valid"

    async def test_dedupes_entities_case_insensitively(self, section) -> None:
        provider = MagicMock()
        provider.complete = AsyncMock(
            return_value=Completion(
                text='{"facts": [{"text": "f", "fact_type": "world",'
                ' "entities": ["Dr. Patel", "dr. patel", "DR. PATEL"]}]}',
                model="gpt-4o-mini",
            )
        )
        facts = await extract_facts_for_section(
            provider,
            section,
            "text",
            bank_id="b1",
        )
        assert facts[0].entities == ["Dr. Patel"]

    async def test_caps_at_12_facts(self, section) -> None:
        many = ",".join(f'{{"text": "f{i}", "fact_type": "world", "entities": []}}' for i in range(20))
        provider = MagicMock()
        provider.complete = AsyncMock(
            return_value=Completion(
                text=f'{{"facts": [{many}]}}',
                model="gpt-4o-mini",
            )
        )
        facts = await extract_facts_for_section(
            provider,
            section,
            "text",
            bank_id="b1",
        )
        assert len(facts) == 12

    async def test_empty_text_returns_empty(self, section) -> None:
        provider = MagicMock()
        facts = await extract_facts_for_section(
            provider,
            section,
            "   ",
            bank_id="b1",
        )
        assert facts == []
        provider.complete.assert_not_called() if hasattr(provider.complete, "assert_not_called") else None

    async def test_json_parse_failure_returns_empty(self, section) -> None:
        provider = MagicMock()
        provider.complete = AsyncMock(
            return_value=Completion(
                text="not valid",
                model="gpt-4o-mini",
            )
        )
        facts = await extract_facts_for_section(
            provider,
            section,
            "text",
            bank_id="b1",
        )
        assert facts == []

    async def test_llm_failure_returns_empty(self, section) -> None:
        provider = MagicMock()
        provider.complete = AsyncMock(side_effect=RuntimeError("api down"))
        facts = await extract_facts_for_section(
            provider,
            section,
            "text",
            bank_id="b1",
        )
        assert facts == []

    async def test_tolerant_parse_strips_markdown_fence(self, section) -> None:
        """The LLM occasionally wraps its JSON in ```` ```json ``` ```` despite
        ``response_format={"type": "json_object"}``. The tolerant parser
        strips the fence and recovers the facts instead of dropping them.
        """
        provider = MagicMock()
        provider.complete = AsyncMock(
            return_value=Completion(
                text=(
                    "```json\n"
                    '{"facts": [{"text": "User did X.", "fact_type": "experience",'
                    ' "entities": []}]}\n'
                    "```"
                ),
                model="gpt-4o-mini",
            )
        )
        facts = await extract_facts_for_section(
            provider, section, "text", bank_id="b1"
        )
        assert len(facts) == 1
        assert facts[0].text == "User did X."
        # No retry should have been issued — the fence parse succeeded.
        assert provider.complete.call_count == 1

    async def test_retry_on_malformed_then_valid(self, section) -> None:
        """If the first response is unparseable AND not budget-truncated,
        the extractor retries once with a stricter system reminder. A
        valid retry response populates the result instead of falling
        through to ``[]``.
        """
        provider = MagicMock()
        provider.complete = AsyncMock(
            side_effect=[
                # First: prose that doesn't contain any JSON braces — not
                # truncated, but unparseable. Forces the retry path.
                Completion(text="Sorry, I cannot do that.", model="gpt-4o-mini"),
                Completion(
                    text='{"facts": [{"text": "ok", "fact_type": "world", "entities": []}]}',
                    model="gpt-4o-mini",
                ),
            ]
        )
        facts = await extract_facts_for_section(
            provider, section, "text", bank_id="b1"
        )
        assert provider.complete.call_count == 2
        assert len(facts) == 1
        assert facts[0].text == "ok"

    async def test_no_retry_when_truncated(self, section) -> None:
        """If the first response looks budget-truncated, a retry under
        the same cap can't help — the extractor must skip it to avoid a
        wasted round-trip.
        """
        provider = MagicMock()
        provider.complete = AsyncMock(
            return_value=Completion(
                text='{"facts": [{"text": "User did',  # truncated mid-string
                model="gpt-4o-mini",
            )
        )
        facts = await extract_facts_for_section(
            provider, section, "text", bank_id="b1"
        )
        assert facts == []
        assert provider.complete.call_count == 1

    async def test_m29_pronoun_resolution_within_section(self, section) -> None:
        """M29 — the extraction prompt instructs the LLM to resolve
        pronouns ("she/he/they") to the most recently named entity
        within the section. This is a contract test that verifies the
        dataclass field-flow preserves the resolved name in fact.text
        when the LLM emits it (i.e. the prompt's instruction round-trips
        through the parser).

        Real LLM compliance with the prompt is bench-verified, not unit-
        verified — this guards against a future regression that strips
        the resolved name (e.g. an over-eager pronoun normalizer).
        """
        provider = MagicMock()
        provider.complete = AsyncMock(
            return_value=Completion(
                text='{"facts": ['
                # LLM resolved "she" → "Emily" in the fact text per the M29 rule.
                '{"text": "Emily said Emily would be home late tonight.",'
                ' "fact_type": "experience", "speaker": "user",'
                ' "occurred_start": null, "occurred_end": null,'
                ' "entities": ["Emily"]}'
                "]}",
                model="gpt-4o-mini",
            )
        )
        facts = await extract_facts_for_section(
            provider, section, "Emily said she would be home late tonight.", bank_id="b1"
        )
        assert len(facts) == 1
        # Pronoun resolved to the named entity in fact text.
        assert "Emily" in facts[0].text
        assert " she " not in facts[0].text
        # Entity array contains the resolved name, not the pronoun.
        assert facts[0].entities == ["Emily"]

    async def test_m29_alias_canonicalization(self, section) -> None:
        """M29 — when both a generic reference ("my roommate") and a
        name ("Emily") appear for the same referent, the entities array
        uses the canonical ``"Name (descriptor)"`` form so cross-section
        link expansion (M27) can stitch sessions that only use one form.

        Verifies the dedup pass preserves the canonical parenthetical
        form rather than collapsing it to a bare name.
        """
        provider = MagicMock()
        provider.complete = AsyncMock(
            return_value=Completion(
                text='{"facts": ['
                '{"text": "Emily, the user\'s roommate, mentioned she would be home late.",'
                ' "fact_type": "experience", "speaker": "user",'
                ' "occurred_start": null, "occurred_end": null,'
                ' "entities": ["Emily (user\'s roommate)", "role:roommate"]}'
                "]}",
                model="gpt-4o-mini",
            )
        )
        facts = await extract_facts_for_section(
            provider,
            section,
            "My roommate Emily came home. Emily said she'd be late tomorrow too.",
            bank_id="b1",
        )
        assert len(facts) == 1
        # Canonical "Name (descriptor)" form survives the parser + dedup pass.
        assert "Emily (user's roommate)" in facts[0].entities
        # Role label also present so role-only references in other sections can link.
        assert "role:roommate" in facts[0].entities

    async def test_m29_role_label_for_generic_reference(self, section) -> None:
        """M29 — when only the role appears (no name in this section),
        the entities array must still include the ``role:<label>``
        token so cross-section link expansion can join on it and
        surface the named version from a different session.
        """
        provider = MagicMock()
        provider.complete = AsyncMock(
            return_value=Completion(
                text='{"facts": ['
                '{"text": "The doctor recommended an antihistamine for the user\'s allergies.",'
                ' "fact_type": "experience", "speaker": "user",'
                ' "occurred_start": null, "occurred_end": null,'
                ' "entities": ["role:doctor", "antihistamine"]}'
                "]}",
                model="gpt-4o-mini",
            )
        )
        facts = await extract_facts_for_section(
            provider,
            section,
            "I saw the doctor today. She gave me an antihistamine for my allergies.",
            bank_id="b1",
        )
        assert len(facts) == 1
        # Role label present so future sections with "Dr. Patel" can link via role:doctor.
        assert "role:doctor" in facts[0].entities


class TestInMemoryFactStore:
    @pytest.fixture
    async def store_with_facts(self) -> tuple[InMemoryPageIndexStore, str]:
        s = InMemoryPageIndexStore()
        doc = PageIndexDocument(
            id="",
            bank_id="b1",
            source_id="s1",
            md_text="# m",
            reference_date=datetime(2023, 5, 8, tzinfo=timezone.utc),
            built_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
        )
        doc_id = await s.save_document(doc)
        await s.save_sections(doc_id, [_section(1, "s1"), _section(5, "s5")])
        facts = [
            PageIndexFact(
                id="f1",
                bank_id="b1",
                document_id=doc_id,
                line_num=1,
                text="User visited Dr. Patel",
                fact_type="experience",
                speaker="user",
                occurred_start=datetime(2023, 5, 7),
                entities=["Dr. Patel", "role:doctor"],
            ),
            PageIndexFact(
                id="f2",
                bank_id="b1",
                document_id=doc_id,
                line_num=5,
                text="User prefers Sony cameras",
                fact_type="preference",
                speaker="user",
                entities=["Sony", "category:camera"],
            ),
            PageIndexFact(
                id="f3",
                bank_id="b1",
                document_id=doc_id,
                line_num=5,
                text="User visited Dr. Lee",
                fact_type="experience",
                speaker="user",
                occurred_start=datetime(2023, 6, 10),
                entities=["Dr. Lee", "role:doctor"],
            ),
        ]
        await s.save_facts(facts)
        return s, doc_id

    async def test_save_facts_persists(self, store_with_facts) -> None:
        s, doc_id = store_with_facts
        # internal check — three facts now stored
        assert len(s._facts.get(doc_id, [])) == 3

    async def test_count_facts_by_entity_pattern(self, store_with_facts) -> None:
        s, doc_id = store_with_facts
        # Two "role:doctor" mentions
        assert (
            await s.count_facts_matching(
                "b1",
                doc_id,
                entity_pattern="role:doctor",
            )
            == 2
        )

    async def test_count_facts_by_type(self, store_with_facts) -> None:
        s, doc_id = store_with_facts
        assert (
            await s.count_facts_matching(
                "b1",
                doc_id,
                fact_type="experience",
            )
            == 2
        )
        assert (
            await s.count_facts_matching(
                "b1",
                doc_id,
                fact_type="preference",
            )
            == 1
        )

    async def test_search_facts_by_entity(self, store_with_facts) -> None:
        s, doc_id = store_with_facts
        hits = await s.search_facts_by_entity("b1", "role:doctor")
        assert len(hits) == 2
        assert {h.text for h in hits} == {
            "User visited Dr. Patel",
            "User visited Dr. Lee",
        }

    async def test_search_facts_temporal(self, store_with_facts) -> None:
        s, doc_id = store_with_facts
        hits = await s.search_facts_temporal(
            "b1",
            (datetime(2023, 5, 1), datetime(2023, 5, 31)),
        )
        assert len(hits) == 1
        assert hits[0].fact_id == "f1"

    async def test_update_fact_embeddings(self, store_with_facts) -> None:
        s, doc_id = store_with_facts
        updated = await s.update_fact_embeddings(
            [
                ("f1", [0.1, 0.2, 0.3]),
                ("f2", [0.4, 0.5, 0.6]),
            ]
        )
        assert updated == 2
        # semantic search now returns f1 / f2 (have embeddings); f3 (None) excluded
        hits = await s.search_facts_semantic("b1", [0.1, 0.2, 0.3], top_k=5)
        ids = {h.fact_id for h in hits}
        assert ids == {"f1", "f2"}
