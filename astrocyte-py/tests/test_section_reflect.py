"""Tests for ``astrocyte.pipeline.section_reflect``.

Three layers:

1. **Memory-id round-trip** — ``format_section_memory_id`` and
   ``parse_section_memory_id`` must be inverses, and the parser must
   degrade gracefully on malformed input (the agentic reflect loop
   feeds us whatever the LLM produced as ``cited_ids``).

2. **Citation filtering** — ``cited_ids_to_line_nums`` must dedupe and
   filter cross-document citations. The bench builds one document per
   question; cross-doc citations indicate the agent confused itself.

3. **Closure factories** — ``make_section_recall_fn`` and
   ``make_section_expand_fn`` must produce callables with the agentic
   reflect loop's expected (query, max_results) → list[MemoryHit] /
   (memory_id, max_sources) → list[MemoryHit] contracts. Tests use
   ``InMemoryPageIndexStore`` so they're fast and dep-free.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from astrocyte.pipeline.section_reflect import (
    cited_ids_to_line_nums,
    format_section_memory_id,
    make_list_entities_fn,
    make_section_expand_fn,
    make_section_recall_fn,
    parse_section_memory_id,
    section_tuples_to_memory_hits,
)
from astrocyte.testing.in_memory import (
    InMemoryPageIndexStore,
    MockLLMProvider,
)
from astrocyte.types import (
    PageIndexDocument,
    PageIndexSection,
    PageIndexSectionLink,
)

# ---------------------------------------------------------------------------
# Memory-id round-trip
# ---------------------------------------------------------------------------


class TestSectionMemoryId:
    def test_format_then_parse_round_trips(self):
        for doc_id, line in [
            ("doc-1", 0),
            ("conv-26", 42),
            ("uuid-abc-def-0123", 1_234_567),
        ]:
            mid = format_section_memory_id(doc_id, line)
            parsed = parse_section_memory_id(mid)
            assert parsed == (doc_id, line)

    def test_parse_handles_doc_ids_containing_colons(self):
        """``rfind(":")`` splits on the LAST colon, so doc_ids like
        ``urn:isbn:0451450523`` are parsed correctly even though they
        contain colons themselves."""
        mid = format_section_memory_id("urn:isbn:0451450523", 7)
        assert parse_section_memory_id(mid) == ("urn:isbn:0451450523", 7)

    @pytest.mark.parametrize(
        "garbage",
        [
            "",  # empty
            "no-colon",  # no separator
            ":42",  # empty doc_id
            "doc:notanint",  # non-integer line
            "doc:",  # missing line_num
        ],
    )
    def test_parse_returns_none_on_malformed_input(self, garbage):
        assert parse_section_memory_id(garbage) is None


# ---------------------------------------------------------------------------
# cited_ids_to_line_nums — dedupe + cross-doc filter
# ---------------------------------------------------------------------------


class TestCitedIdsToLineNums:
    def test_dedupes_same_line_repeated(self):
        ids = [
            format_section_memory_id("d1", 5),
            format_section_memory_id("d1", 5),  # duplicate
            format_section_memory_id("d1", 9),
        ]
        assert cited_ids_to_line_nums(ids) == [5, 9]

    def test_filters_cross_document_citations(self):
        """``expected_doc_id`` set → only line_nums from that doc survive."""
        ids = [
            format_section_memory_id("d1", 1),
            format_section_memory_id("d2", 2),  # wrong doc — drop
            format_section_memory_id("d1", 3),
        ]
        assert cited_ids_to_line_nums(ids, expected_doc_id="d1") == [1, 3]

    def test_no_filter_returns_all_parseable_lines(self):
        ids = [
            format_section_memory_id("d1", 1),
            format_section_memory_id("d2", 2),
        ]
        assert cited_ids_to_line_nums(ids) == [1, 2]

    def test_skips_unparseable_ids(self):
        """Agent-invented ids (no colon, etc.) are filtered, not raised."""
        ids = [
            "this-is-garbage",
            format_section_memory_id("d1", 7),
            "",
        ]
        assert cited_ids_to_line_nums(ids) == [7]


# ---------------------------------------------------------------------------
# section_tuples_to_memory_hits — pure conversion
# ---------------------------------------------------------------------------


class TestSectionTuplesToMemoryHits:
    def test_truncates_text_at_max_chars(self):
        big_md = "X" * 2_000

        def slice_all(md, line):
            return md  # ignore line; return everything

        hits = section_tuples_to_memory_hits(
            [("d1", 0, 0.7)],
            md_text_by_doc={"d1": big_md},
            slice_fn=slice_all,
            max_chars=100,
        )
        assert len(hits) == 1
        assert len(hits[0].text) == 100
        assert hits[0].text.endswith("...")
        assert hits[0].memory_id == "d1:0"
        assert hits[0].score == 0.7

    def test_missing_doc_yields_empty_text(self):
        """When ``md_text_by_doc`` doesn't have the doc, text is empty
        but the hit is still returned (caller decides whether to drop)."""
        hits = section_tuples_to_memory_hits(
            [("missing", 5, 0.9)],
            md_text_by_doc={},
            slice_fn=lambda md, ln: md,
        )
        assert len(hits) == 1
        assert hits[0].text == ""
        assert hits[0].memory_id == "missing:5"


# ---------------------------------------------------------------------------
# Closure factories — smoke test against InMemoryPageIndexStore
# ---------------------------------------------------------------------------


@pytest.fixture
async def populated_store():
    """One document with three sections; section 1 is linked to section 2."""
    store = InMemoryPageIndexStore()
    doc = PageIndexDocument(
        id="",
        bank_id="b1",
        source_id="conv-1",
        md_text="line 1\nline 2\nline 3\n",
        reference_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        built_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    doc_id = await store.save_document(doc)

    sections = [
        PageIndexSection(
            document_id=doc_id,
            line_num=ln,
            node_id=f"{ln:04d}",
            title=f"Section {ln}",
            summary=f"Summary for line {ln}",
            depth=1,
        )
        for ln in (1, 2, 3)
    ]
    await store.save_sections(doc_id, sections)

    # Link line 1 → line 2 (semantic_knn, weight 0.9)
    await store.save_section_links(
        [
            PageIndexSectionLink(
                from_doc=doc_id,
                from_line=1,
                to_doc=doc_id,
                to_line=2,
                link_type="semantic_knn",
                weight=0.9,
            ),
        ]
    )
    return store, doc_id


def _slice_fn(md_text: str, line_num: int) -> str:
    """Test slice helper — return the matching markdown line."""
    lines = md_text.split("\n")
    if 1 <= line_num <= len(lines):
        return lines[line_num - 1]
    return ""


class TestMakeSectionRecallFn:
    @pytest.mark.asyncio
    async def test_returns_callable_with_recall_fn_shape(self, populated_store):
        store, doc_id = populated_store
        recall_fn = make_section_recall_fn(
            store=store,
            bank_id="b1",
            embedding_provider=MockLLMProvider(),
            md_text_by_doc={doc_id: "line 1\nline 2\nline 3\n"},
            slice_fn=_slice_fn,
        )
        # Contract: takes (query, max_results), returns list[MemoryHit].
        hits = await recall_fn("anything", 5)
        assert isinstance(hits, list)
        for h in hits:
            assert h.memory_id is not None
            assert ":" in h.memory_id  # format_section_memory_id shape

    @pytest.mark.asyncio
    async def test_failure_returns_empty_not_raise(self):
        """If the underlying section_recall raises, the closure must
        return [] so the agentic loop degrades gracefully — same
        convention as the recall_result upstream. No fixture needed —
        we wire BrokenStore directly."""

        class BrokenStore:
            """Raises on every method call."""

            async def search_sections_semantic(self, *a, **kw):
                raise RuntimeError("synthetic failure")

            async def search_sections_keyword(self, *a, **kw):
                raise RuntimeError("synthetic failure")

            async def search_sections_by_entities(self, *a, **kw):
                raise RuntimeError("synthetic failure")

            async def search_sections_temporal(self, *a, **kw):
                raise RuntimeError("synthetic failure")

            async def expand_section_links(self, *a, **kw):
                raise RuntimeError("synthetic failure")

        recall_fn = make_section_recall_fn(
            store=BrokenStore(),  # type: ignore[arg-type]
            bank_id="b1",
            embedding_provider=MockLLMProvider(),
            md_text_by_doc={},
            slice_fn=_slice_fn,
        )
        hits = await recall_fn("anything", 5)
        assert hits == []


class TestMakeSectionExpandFn:
    @pytest.mark.asyncio
    async def test_expands_through_section_links(self, populated_store):
        store, doc_id = populated_store
        expand_fn = make_section_expand_fn(
            store=store,
            md_text_by_doc={doc_id: "line 1\nline 2\nline 3\n"},
            slice_fn=_slice_fn,
        )
        seed = format_section_memory_id(doc_id, 1)
        hits = await expand_fn(seed, max_sources=5)
        # Section 1 → Section 2 link; expansion should surface line 2.
        assert any(h.memory_id == format_section_memory_id(doc_id, 2) for h in hits)

    @pytest.mark.asyncio
    async def test_unparseable_seed_returns_empty(self, populated_store):
        """The agent occasionally hands us garbage memory_ids. Must
        return [] without touching the store."""
        store, _ = populated_store
        expand_fn = make_section_expand_fn(
            store=store,
            md_text_by_doc={},
            slice_fn=_slice_fn,
        )
        assert await expand_fn("garbage-no-colon", 5) == []
        assert await expand_fn("", 5) == []


class TestMakeListEntitiesFn:
    """The PR2.6 ``list_entities`` agentic-reflect tool: bench-side
    closure factory that scopes ``PageIndexStore.list_distinct_entities``
    to one document. The agent calls it with ``(pattern, limit)``;
    document_id is bound at factory time."""

    @pytest.mark.asyncio
    async def test_returns_callable_with_pattern_filter_shape(
        self,
        populated_store,
    ):
        """The factory binds bank_id + document_id and exposes a
        ``(pattern, limit) -> [(name, count)]`` callable."""
        from astrocyte.types import PageIndexSectionEntity

        store, doc_id = populated_store
        # Seed 3 distinct entities with different mention counts so the
        # factory's output ordering (count desc, name asc) is testable.
        await store.save_section_entities(
            [
                PageIndexSectionEntity(document_id=doc_id, line_num=1, entity_name="Alice"),
                PageIndexSectionEntity(document_id=doc_id, line_num=2, entity_name="Alice"),
                PageIndexSectionEntity(document_id=doc_id, line_num=3, entity_name="Alice"),
                PageIndexSectionEntity(document_id=doc_id, line_num=1, entity_name="Bob"),
                PageIndexSectionEntity(document_id=doc_id, line_num=2, entity_name="Bob"),
                PageIndexSectionEntity(document_id=doc_id, line_num=1, entity_name="Carol"),
            ]
        )

        list_entities_fn = make_list_entities_fn(
            store=store,
            bank_id="b1",
            document_id=doc_id,
        )
        # No pattern → top-N entities by mention count.
        results = await list_entities_fn(None, 10)
        names = [name for name, _count in results]
        # Alice (3) > Bob (2) > Carol (1) — count desc, name asc.
        assert names[0] == "Alice"
        assert names[1] == "Bob"
        assert names[2] == "Carol"

        # Pattern filter ("ali") → only Alice (case-insensitive).
        results = await list_entities_fn("ali", 10)
        assert [name for name, _count in results] == ["Alice"]

    @pytest.mark.asyncio
    async def test_failure_returns_empty_not_raise(self):
        """If the underlying store raises, the closure must return []
        so the agentic loop degrades gracefully — same convention as
        make_section_recall_fn / make_section_expand_fn."""

        class BrokenStore:
            async def list_distinct_entities(self, *a, **kw):
                raise RuntimeError("synthetic list_distinct_entities failure")

        list_entities_fn = make_list_entities_fn(
            store=BrokenStore(),  # type: ignore[arg-type]
            bank_id="b1",
            document_id="doc-1",
        )
        assert await list_entities_fn("anything", 10) == []
        assert await list_entities_fn(None, 10) == []
