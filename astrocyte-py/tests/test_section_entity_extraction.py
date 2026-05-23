"""Tests for ``astrocyte.pipeline.section_entity_extraction`` (PR2 commit A).

Pinned behaviours:
- Single LLM call per section returns up to 15 ``PageIndexSectionEntity``
  rows.
- Empty / whitespace text → empty result, no API call.
- JSON parse failures degrade silently (returns []; the picker keeps
  working without entity rows for this section).
- Case-insensitive dedupe: "Caroline" and "caroline" collapse to one
  row, first-seen casing wins.
- 15-entity cap prevents pathological extractions (lyric quotations,
  recipe ingredients) from blowing up the index.
- Whitespace-stripped names; empty strings filtered.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from astrocyte.pipeline.section_entity_extraction import (
    extract_entities_for_section,
)
from astrocyte.types import (
    Message,
    PageIndexSection,
    PageIndexSectionEntity,
)


@dataclass
class _Completion:
    text: str


@dataclass
class _StubProvider:
    """Stub LLM provider for entity extraction tests. Records the
    rendered prompt so we can assert against the input shape."""

    response_text: str
    raise_exc: Exception | None = None
    prompts_seen: list[str] = None
    # When set, successive calls pop from this list (so we can simulate
    # the retry path: malformed first, valid second).
    response_sequence: list[str] | None = None

    def __post_init__(self) -> None:
        self.prompts_seen = []
        self.call_count = 0

    async def complete(self, **kwargs: Any) -> _Completion:
        msgs = kwargs.get("messages") or []
        # The retry path prepends a system message; the user prompt is
        # the last message in both shapes. Record it.
        for m in msgs:
            if isinstance(m, Message) and m.role == "user":
                self.prompts_seen.append(m.content)
                break
        self.call_count += 1
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.response_sequence is not None:
            idx = min(self.call_count - 1, len(self.response_sequence) - 1)
            return _Completion(text=self.response_sequence[idx])
        return _Completion(text=self.response_text)


def _section(line_num: int) -> PageIndexSection:
    return PageIndexSection(
        document_id="doc-1",
        line_num=line_num,
        node_id=f"{line_num:04d}",
        title=f"Session {line_num}",
        depth=1,
    )


class TestExtractEntitiesForSection:
    async def test_happy_path_returns_section_entities(self) -> None:
        provider = _StubProvider(
            response_text=json.dumps({"entities": ["Alice", "Google", "Python"]}),
        )
        section = _section(line_num=5)
        result = await extract_entities_for_section(
            provider,
            "doc-1",
            section,
            "Alice works at Google on Python.",
        )
        assert len(result) == 3
        assert all(isinstance(e, PageIndexSectionEntity) for e in result)
        assert {e.entity_name for e in result} == {"Alice", "Google", "Python"}
        assert all(e.document_id == "doc-1" and e.line_num == 5 for e in result)

    async def test_empty_text_no_api_call(self) -> None:
        provider = _StubProvider(response_text="never")
        result = await extract_entities_for_section(
            provider,
            "doc-1",
            _section(5),
            "",
        )
        assert result == []
        assert provider.prompts_seen == []

    async def test_whitespace_text_no_api_call(self) -> None:
        provider = _StubProvider(response_text="never")
        result = await extract_entities_for_section(
            provider,
            "doc-1",
            _section(5),
            "   \n\t  ",
        )
        assert result == []
        assert provider.prompts_seen == []

    async def test_json_parse_failure_returns_empty(self) -> None:
        # LLM returned non-JSON. Picker degrades gracefully.
        provider = _StubProvider(response_text="oops not json {")
        result = await extract_entities_for_section(
            provider,
            "doc-1",
            _section(5),
            "Alice and Bob talked.",
        )
        assert result == []

    async def test_missing_entities_key_returns_empty(self) -> None:
        # Valid JSON but no ``entities`` key.
        provider = _StubProvider(response_text=json.dumps({"answer": "no entities"}))
        result = await extract_entities_for_section(
            provider,
            "doc-1",
            _section(5),
            "...",
        )
        assert result == []

    async def test_case_insensitive_dedupe(self) -> None:
        # "Caroline" and "caroline" → one row. First-seen casing wins.
        provider = _StubProvider(
            response_text=json.dumps(
                {
                    "entities": ["Caroline", "caroline", "CAROLINE", "Bob"],
                }
            ),
        )
        result = await extract_entities_for_section(
            provider,
            "doc-1",
            _section(5),
            "...",
        )
        names = [e.entity_name for e in result]
        assert names == ["Caroline", "Bob"]

    async def test_caps_at_20_entities(self) -> None:
        # Pathological extraction (e.g. recipe with 30 ingredients).
        # Cap prevents the index from being dominated by one section.
        # M17 follow-up: cap raised from 15 → 20 to make room for the
        # alias-capture variants (typical inflation is 1-2 aliases per
        # person mentioned).
        provider = _StubProvider(
            response_text=json.dumps(
                {
                    "entities": [f"Entity{i}" for i in range(30)],
                }
            ),
        )
        result = await extract_entities_for_section(
            provider,
            "doc-1",
            _section(5),
            "...",
        )
        assert len(result) == 20
        assert [e.entity_name for e in result] == [f"Entity{i}" for i in range(20)]

    async def test_strips_whitespace_and_filters_empties(self) -> None:
        provider = _StubProvider(
            response_text=json.dumps(
                {
                    "entities": ["  Alice  ", "", "   ", "\nBob\t"],
                }
            ),
        )
        result = await extract_entities_for_section(
            provider,
            "doc-1",
            _section(5),
            "...",
        )
        assert [e.entity_name for e in result] == ["Alice", "Bob"]

    async def test_non_string_entries_filtered(self) -> None:
        # Defensive: LLM occasionally returns mixed types.
        provider = _StubProvider(
            response_text=json.dumps(
                {
                    "entities": ["Alice", 42, None, {"name": "Bob"}, "Charlie"],
                }
            ),
        )
        result = await extract_entities_for_section(
            provider,
            "doc-1",
            _section(5),
            "...",
        )
        assert [e.entity_name for e in result] == ["Alice", "Charlie"]

    async def test_tolerant_parse_strips_markdown_fence(self) -> None:
        """gpt-4o-mini sometimes wraps the JSON in ```` ```json ``` ```` fences;
        the tolerant parser strips them so the entities aren't lost."""
        provider = _StubProvider(
            response_text='```json\n{"entities": ["Alice", "Bob"]}\n```',
        )
        result = await extract_entities_for_section(
            provider, "doc-1", _section(5), "Alice and Bob talked."
        )
        assert {e.entity_name for e in result} == {"Alice", "Bob"}
        assert provider.call_count == 1  # no retry needed

    async def test_retry_on_malformed_then_valid(self) -> None:
        """Unparseable first response (not truncated) triggers exactly
        one retry; a valid retry result populates the entities."""
        provider = _StubProvider(
            response_text="",
            response_sequence=[
                "I cannot do that.",  # not JSON-ish, not truncated
                json.dumps({"entities": ["Alice"]}),
            ],
        )
        result = await extract_entities_for_section(
            provider, "doc-1", _section(5), "Alice talked."
        )
        assert provider.call_count == 2
        assert [e.entity_name for e in result] == ["Alice"]

    async def test_no_retry_when_truncated(self) -> None:
        """Budget-truncated first response (last char is mid-string)
        must NOT trigger a retry — the retry would hit the same cap."""
        provider = _StubProvider(
            response_text='{"entities": ["Alic',  # truncated
        )
        result = await extract_entities_for_section(
            provider, "doc-1", _section(5), "Alice talked."
        )
        assert result == []
        assert provider.call_count == 1

    async def test_truncates_long_section_text(self) -> None:
        # Defensive: section_text is capped at 6K chars before being
        # interpolated into the prompt. Pin so the prompt doesn't blow
        # the LLM context window on pathologically large sections.
        provider = _StubProvider(response_text=json.dumps({"entities": []}))
        # Use a marker char ('Q') that isn't in the prompt template.
        long_text = "Q" * 20_000
        await extract_entities_for_section(
            provider,
            "doc-1",
            _section(5),
            long_text,
        )
        prompt = provider.prompts_seen[0]
        assert prompt.count("Q") == 6_000  # the cap is exact (string slice)


class TestInMemoryStoreSaveSectionEmbeddings:
    """Pin the InMemoryPageIndexStore.save_section_embeddings semantics
    added in PR2 commit A. Mirrors the Postgres adapter's UPDATE shape."""

    async def test_updates_existing_sections(self) -> None:
        from datetime import datetime, timezone

        from astrocyte.testing.in_memory import InMemoryPageIndexStore
        from astrocyte.types import PageIndexDocument

        store = InMemoryPageIndexStore()
        doc = PageIndexDocument(
            id="",
            bank_id="b1",
            source_id="conv-1",
            md_text="x",
            reference_date=None,
            built_at=datetime.now(tz=timezone.utc),
        )
        doc_id = await store.save_document(doc)
        sections = [_section(1), _section(5), _section(10)]
        for s in sections:
            s.document_id = doc_id
        await store.save_sections(doc_id, sections)

        # Update embeddings on lines 1 and 10; line 5 untouched
        n = await store.save_section_embeddings(doc_id, [(1, [0.1] * 4), (10, [0.3] * 4)])
        assert n == 2

        # load_skeleton must succeed even though we inspect via the
        # internal store below — the picker projects out summary_embedding
        # for cheaper reads, but PR2's strategy SQL queries embeddings
        # separately, so we verify the embeddings ARE persisted by
        # touching ``store._sections`` directly.
        await store.load_skeleton(doc_id)
        for s in store._sections[doc_id]:
            if s.line_num in (1, 10):
                assert s.summary_embedding is not None
            else:
                assert s.summary_embedding is None

    async def test_skips_unknown_line_nums(self) -> None:
        # Defensive: don't auto-create sections from embedding writes.
        # Tree-build is the source of truth for which sections exist.
        from datetime import datetime, timezone

        from astrocyte.testing.in_memory import InMemoryPageIndexStore
        from astrocyte.types import PageIndexDocument

        store = InMemoryPageIndexStore()
        doc = PageIndexDocument(
            id="",
            bank_id="b1",
            source_id="conv-1",
            md_text="x",
            reference_date=None,
            built_at=datetime.now(tz=timezone.utc),
        )
        doc_id = await store.save_document(doc)
        s1 = _section(1)
        s1.document_id = doc_id
        await store.save_sections(doc_id, [s1])
        # Line 999 doesn't exist; should be skipped, not created
        n = await store.save_section_embeddings(doc_id, [(1, [0.1] * 3), (999, [0.9] * 3)])
        assert n == 1
        assert len(store._sections[doc_id]) == 1

    async def test_empty_embeddings_noop(self) -> None:
        from astrocyte.testing.in_memory import InMemoryPageIndexStore

        store = InMemoryPageIndexStore()
        assert await store.save_section_embeddings("nonexistent-doc", []) == 0
