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

    async def test_assistant_statement_extracted(self, section) -> None:
        """M14.1: ``assistant_statement`` is a first-class fact_type.

        Targets LME single-session-assistant lift — when the assistant
        says something substantive, the extractor produces a row
        preserving the assistant's phrasing so the answerer can quote it
        directly.
        """
        provider = MagicMock()
        provider.complete = AsyncMock(
            return_value=Completion(
                text='{"facts": ['
                '{"text": "Assistant recommended a saline rinse for post-nasal drip.",'
                ' "fact_type": "assistant_statement", "speaker": "assistant",'
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
        assert facts[0].fact_type == "assistant_statement"
        assert facts[0].speaker == "assistant"
        assert "saline rinse" in facts[0].entities

    async def test_mixed_user_and_assistant_facts(self, section) -> None:
        """Multi-output: one section yields rows of multiple fact_types
        including assistant_statement — captures the realistic shape of
        a back-and-forth dialogue turn.
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
                ' "fact_type": "assistant_statement", "speaker": "assistant",'
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
        assert fact_types == {"experience", "preference", "assistant_statement"}

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
