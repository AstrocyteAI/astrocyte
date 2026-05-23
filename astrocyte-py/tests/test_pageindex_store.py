"""Tests for the M9 ``PageIndexStore`` SPI (PR1 commit E).

Two layers:

1. ``TestInMemoryPageIndexStore`` — black-box conformance against the
   reference fixture. These tests are also the spec for any future
   adapter (Postgres, in-tree mock, third-party). Behaviours pinned:

   - upsert keyed on ``(bank_id, source_id)`` (rebuild replaces in place)
   - ``save_sections`` is atomic-replace (cascades wipe entities/links)
   - ``load_skeleton`` projects out ``summary_embedding`` (PR2 column;
     picker doesn't need it)
   - entity / link upserts are idempotent on their composite keys
   - ``Protocol`` runtime check passes (interface contract)

2. ``TestBenchHarnessConversion`` — the bench harness's
   ``_flatten_tree_to_sections`` ⇄ ``_sections_to_compact_tree`` round-
   trip. Pinned because the picker prompt's nested-dict shape is what
   the LLM has been tuned against in Phase A v1 → v7; any drift in the
   reconstructed shape silently regresses accuracy.

Postgres store coverage is in
``adapters-storage-py/astrocyte-postgres/tests/test_postgres_pageindex_store.py``
(skipped when ``DATABASE_URL`` is unset).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from astrocyte.provider import PageIndexStore
from astrocyte.testing.in_memory import InMemoryPageIndexStore
from astrocyte.types import (
    PageIndexDocument,
    PageIndexSection,
    PageIndexSectionEntity,
    PageIndexSectionLink,
)

# ── Fixtures ───────────────────────────────────────────────────────────


def _doc(bank_id: str = "b1", source_id: str = "conv-26", md: str = "# md") -> PageIndexDocument:
    return PageIndexDocument(
        id="",  # ignored on insert; store-assigned
        bank_id=bank_id,
        source_id=source_id,
        md_text=md,
        reference_date=datetime(2023, 5, 8, tzinfo=timezone.utc),
        built_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
    )


def _section(
    document_id: str,
    line_num: int,
    *,
    node_id: str | None = None,
    parent_node: str | None = None,
    depth: int = 0,
    title: str | None = None,
    summary: str | None = None,
    session_date: datetime | None = None,
) -> PageIndexSection:
    return PageIndexSection(
        document_id=document_id,
        line_num=line_num,
        node_id=node_id or f"{line_num:04d}",
        title=title or f"node-{line_num}",
        summary=summary,
        speaker=None,
        session_date=session_date,
        parent_node=parent_node,
        depth=depth,
    )


# ── Tests: InMemoryPageIndexStore (the SPI conformance suite) ─────────


class TestInMemoryPageIndexStore:
    """Black-box conformance for the SPI. Any new adapter must pass these
    same tests (point a parametrised fixture at it)."""

    @pytest.fixture
    def store(self) -> InMemoryPageIndexStore:
        return InMemoryPageIndexStore()

    def test_protocol_check(self, store: InMemoryPageIndexStore) -> None:
        # Runtime ``isinstance`` against the Protocol — pins the public
        # method surface. If ``PageIndexStore`` grows a method without
        # the in-memory store implementing it, this fails fast.
        assert isinstance(store, PageIndexStore)

    async def test_save_load_document_roundtrip(self, store: InMemoryPageIndexStore) -> None:
        doc_id = await store.save_document(_doc(md="hello"))
        assert doc_id  # non-empty string

        loaded = await store.load_document("b1", "conv-26")
        assert loaded is not None
        assert loaded.md_text == "hello"
        assert loaded.bank_id == "b1"
        assert loaded.source_id == "conv-26"
        assert loaded.id == doc_id

    async def test_load_document_returns_none_when_missing(
        self,
        store: InMemoryPageIndexStore,
    ) -> None:
        # ``None`` (not exception) is the contract — caller decides
        # whether to build the doc.
        assert await store.load_document("b1", "no-such-conv") is None

    async def test_upsert_keyed_on_bank_id_source_id(
        self,
        store: InMemoryPageIndexStore,
    ) -> None:
        # First save assigns an id; second save with the same
        # (bank_id, source_id) must REPLACE in place and preserve the id
        # (so child rows stay valid).
        first_id = await store.save_document(_doc(md="v1"))
        second_id = await store.save_document(_doc(md="v2"))
        assert first_id == second_id, "upsert must preserve document id"

        loaded = await store.load_document("b1", "conv-26")
        assert loaded is not None
        assert loaded.md_text == "v2", "newer md_text wins"

    async def test_save_sections_atomic_replace(
        self,
        store: InMemoryPageIndexStore,
    ) -> None:
        doc_id = await store.save_document(_doc())
        v1 = [_section(doc_id, 1), _section(doc_id, 5), _section(doc_id, 10)]
        n = await store.save_sections(doc_id, v1)
        assert n == 3
        skel = await store.load_skeleton(doc_id)
        assert [s.line_num for s in skel] == [1, 5, 10]

        # Replace with a smaller tree — old sections must be gone.
        v2 = [_section(doc_id, 100), _section(doc_id, 200)]
        n2 = await store.save_sections(doc_id, v2)
        assert n2 == 2
        skel2 = await store.load_skeleton(doc_id)
        assert [s.line_num for s in skel2] == [100, 200]

    async def test_save_sections_cascades_to_entities_and_links(
        self,
        store: InMemoryPageIndexStore,
    ) -> None:
        # Mirrors the FK ON DELETE CASCADE in migration 015 — atomic
        # tree replace must drop dependent rows, not orphan them.
        doc_id = await store.save_document(_doc())
        await store.save_sections(doc_id, [_section(doc_id, 1), _section(doc_id, 5)])
        await store.save_section_entities(
            [
                PageIndexSectionEntity(document_id=doc_id, line_num=1, entity_name="Alice"),
            ]
        )
        await store.save_section_links(
            [
                PageIndexSectionLink(
                    from_doc=doc_id,
                    from_line=1,
                    to_doc=doc_id,
                    to_line=5,
                    link_type="semantic_knn",
                    weight=0.9,
                ),
            ]
        )
        # Replace the tree.
        await store.save_sections(doc_id, [_section(doc_id, 99)])
        # Old entity/link buckets must be empty (cascade).
        assert store._section_entities.get(doc_id) is None
        assert store._section_links.get(doc_id) is None

    async def test_load_skeleton_orders_by_line_num(
        self,
        store: InMemoryPageIndexStore,
    ) -> None:
        doc_id = await store.save_document(_doc())
        # Insert in scrambled order — load must sort.
        await store.save_sections(
            doc_id,
            [
                _section(doc_id, 50),
                _section(doc_id, 1),
                _section(doc_id, 12),
            ],
        )
        skel = await store.load_skeleton(doc_id)
        assert [s.line_num for s in skel] == [1, 12, 50]

    async def test_load_skeleton_strips_summary_embedding(
        self,
        store: InMemoryPageIndexStore,
    ) -> None:
        # The picker doesn't need embeddings; SQL adapter projects them
        # out to keep the read cheap. In-memory store mirrors that to
        # keep the conformance contract identical.
        doc_id = await store.save_document(_doc())
        s = _section(doc_id, 1)
        s.summary_embedding = [0.1] * 1536
        await store.save_sections(doc_id, [s])
        skel = await store.load_skeleton(doc_id)
        assert skel[0].summary_embedding is None

    async def test_save_section_entities_idempotent(
        self,
        store: InMemoryPageIndexStore,
    ) -> None:
        doc_id = await store.save_document(_doc())
        await store.save_sections(doc_id, [_section(doc_id, 1)])
        e = PageIndexSectionEntity(document_id=doc_id, line_num=1, entity_name="Alice")
        n1 = await store.save_section_entities([e])
        n2 = await store.save_section_entities([e])  # same row
        assert n1 == 1 and n2 == 0, "second insert is a no-op on the composite PK"

    async def test_save_section_links_idempotent(
        self,
        store: InMemoryPageIndexStore,
    ) -> None:
        doc_id = await store.save_document(_doc())
        await store.save_sections(doc_id, [_section(doc_id, 1), _section(doc_id, 5)])
        link = PageIndexSectionLink(
            from_doc=doc_id,
            from_line=1,
            to_doc=doc_id,
            to_line=5,
            link_type="semantic_knn",
            weight=0.85,
        )
        n1 = await store.save_section_links([link])
        n2 = await store.save_section_links([link])
        assert n1 == 1 and n2 == 0

    async def test_save_empty_lists_noop(self, store: InMemoryPageIndexStore) -> None:
        doc_id = await store.save_document(_doc())
        assert await store.save_sections(doc_id, []) == 0
        assert await store.save_section_entities([]) == 0
        assert await store.save_section_links([]) == 0

    async def test_health(self, store: InMemoryPageIndexStore) -> None:
        h = await store.health()
        assert h.healthy is True


# ── Tests: bench harness flatten/reconstruct round-trip ───────────────


# Add the bench script to sys.path so we can import its helpers without
# packaging the script. The bench script does sys.path.insert for the
# PageIndex package; we replicate that here so the import doesn't fail
# on test collection in environments without PageIndex installed.
_PAGEINDEX_ROOT = Path("/Users/calvin/AstrocyteAI/PageIndex")
if str(_PAGEINDEX_ROOT) not in sys.path and _PAGEINDEX_ROOT.exists():
    sys.path.insert(0, str(_PAGEINDEX_ROOT))


def _import_bench_module():
    """Import the bench script as a module so we can call its helpers
    directly without invoking ``main()``. Skipped when PageIndex isn't
    installed (so this file collects clean in CI without PageIndex)."""
    pytest.importorskip("pageindex")
    import importlib.util

    bench_path = Path(__file__).resolve().parent.parent / "scripts" / "bench_pageindex_locomo.py"
    spec = importlib.util.spec_from_file_location("bench_pi_locomo", bench_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bench_module():
    return _import_bench_module()


class TestFactTypeFilterSPI:
    """M34-3 — per-fact-type filter on search_facts_* SPI methods.

    Enables per-fact-type segmented retrieval in M34-4. Each SPI must
    correctly filter by fact_type when provided AND remain
    backward-compatible when not provided (returns cross-type results)."""

    @pytest.fixture
    def store(self):
        return InMemoryPageIndexStore()

    def _fact(self, store, *, fid, ft, text="", mentioned_at=None, entities=None):
        from astrocyte.types import MemoryFact

        f = MemoryFact(
            id=fid, bank_id="b1", text=text, fact_type=ft,
            document_id="doc1", line_num=1,
            mentioned_at=mentioned_at,
            entities=entities or [],
        )
        store._facts.setdefault("doc1", []).append(f)

    async def test_search_facts_temporal_filters_by_fact_type(
        self,
        store: InMemoryPageIndexStore,
    ) -> None:
        from datetime import datetime as _dt

        mentioned = _dt(2026, 3, 15)
        self._fact(store, fid="exp", ft="experience", text="went to wedding", mentioned_at=mentioned)
        self._fact(store, fid="pref", ft="preference", text="prefers tea", mentioned_at=mentioned)
        self._fact(store, fid="world", ft="world", text="sky is blue", mentioned_at=mentioned)

        window = (_dt(2026, 3, 10), _dt(2026, 3, 20))
        # No filter → all three
        all_hits = await store.search_facts_temporal("b1", window)
        assert {h.fact_id for h in all_hits} == {"exp", "pref", "world"}
        # fact_type='preference' → only the preference fact
        pref_hits = await store.search_facts_temporal("b1", window, fact_type="preference")
        assert [h.fact_id for h in pref_hits] == ["pref"]

    async def test_search_facts_keyword_filters_by_fact_type(
        self,
        store: InMemoryPageIndexStore,
    ) -> None:
        self._fact(store, fid="exp", ft="experience", text="attended wedding ceremony")
        self._fact(store, fid="pref", ft="preference", text="favourite wedding cake flavor")

        # No filter → both
        all_hits = await store.search_facts_keyword("b1", "wedding")
        assert {h.fact_id for h in all_hits} == {"exp", "pref"}
        # fact_type='experience' → only the experience fact
        exp_hits = await store.search_facts_keyword("b1", "wedding", fact_type="experience")
        assert [h.fact_id for h in exp_hits] == ["exp"]

    async def test_search_facts_by_entity_filters_by_fact_type(
        self,
        store: InMemoryPageIndexStore,
    ) -> None:
        self._fact(store, fid="exp", ft="experience", text="t1", entities=["Sarah"])
        self._fact(store, fid="pref", ft="preference", text="t2", entities=["Sarah"])

        # No filter → both
        all_hits = await store.search_facts_by_entity("b1", "Sarah")
        assert {h.fact_id for h in all_hits} == {"exp", "pref"}
        # fact_type='preference' → only the preference fact
        pref_hits = await store.search_facts_by_entity("b1", "Sarah", fact_type="preference")
        assert [h.fact_id for h in pref_hits] == ["pref"]


class TestSearchFactsTemporal4WayOR:
    """M33-3b — Hindsight-parity 4-way OR temporal recall.

    The legacy filter only matched ``occurred_start BETWEEN start AND
    end``. That misses three large classes of facts:

      1. Spanning events whose ``[occurred_start, occurred_end]``
         contains the query window but neither endpoint is in it
         (e.g. fact says "April 1-30", query is "in April 10-20").
      2. Facts with only ``mentioned_at`` populated (typical in our
         bench corpus where the LLM often omits ``occurred_start``).
      3. Facts with only ``occurred_end`` populated (rare but real:
         "the event ended on Mar 15", no start).

    These tests pin the new behaviour so a future refactor doesn't
    silently regress to the single-column filter.
    """

    @pytest.fixture
    def store(self):
        return InMemoryPageIndexStore()

    @pytest.fixture
    def window(self):
        from datetime import datetime as _dt

        return _dt(2026, 3, 10), _dt(2026, 3, 20)

    def _fact(
        self,
        store: InMemoryPageIndexStore,
        *,
        fid: str,
        text: str,
        occurred_start=None,
        occurred_end=None,
        mentioned_at=None,
    ):
        """Drop a fact directly into the in-memory store. We bypass
        ``save_facts`` to keep the test focused on the recall filter
        rather than the embed-and-persist path."""
        from astrocyte.types import MemoryFact

        f = MemoryFact(
            id=fid,
            bank_id="b1",
            text=text,
            fact_type="experience",
            document_id="doc1",
            line_num=1,
            occurred_start=occurred_start,
            occurred_end=occurred_end,
            mentioned_at=mentioned_at,
        )
        store._facts.setdefault("doc1", []).append(f)
        return f

    async def test_spanning_event_matches_when_window_inside_range(
        self,
        store: InMemoryPageIndexStore,
        window,
    ) -> None:
        # Pre-M33-3b this would MISS: occurred_start (Mar 1) is outside
        # the window (Mar 10-20), and the old SQL only checked
        # occurred_start BETWEEN start AND end.
        from datetime import datetime as _dt

        self._fact(
            store,
            fid="f-span",
            text="conference ran Mar 1 to Mar 30",
            occurred_start=_dt(2026, 3, 1),
            occurred_end=_dt(2026, 3, 30),
        )
        hits = await store.search_facts_temporal("b1", window)
        assert [h.fact_id for h in hits] == ["f-span"]

    async def test_mentioned_at_only_matches(
        self,
        store: InMemoryPageIndexStore,
        window,
    ) -> None:
        # Typical bench fact: LLM didn't extract occurred_start, but
        # ``mentioned_at`` is the section's session_date — which lands
        # inside the query window. The fact MUST surface.
        from datetime import datetime as _dt

        self._fact(
            store,
            fid="f-mentioned",
            text="user said this on Mar 15",
            mentioned_at=_dt(2026, 3, 15),
        )
        hits = await store.search_facts_temporal("b1", window)
        assert [h.fact_id for h in hits] == ["f-mentioned"]

    async def test_occurred_end_only_matches(
        self,
        store: InMemoryPageIndexStore,
        window,
    ) -> None:
        # Edge case: end populated but no start. Hindsight catches this
        # via the dedicated occurred_end IN range branch.
        from datetime import datetime as _dt

        self._fact(
            store,
            fid="f-end-only",
            text="finished on Mar 15",
            occurred_end=_dt(2026, 3, 15),
        )
        hits = await store.search_facts_temporal("b1", window)
        assert [h.fact_id for h in hits] == ["f-end-only"]

    async def test_occurred_start_in_range_still_matches(
        self,
        store: InMemoryPageIndexStore,
        window,
    ) -> None:
        # The pre-M33-3b code path is preserved as one of the 4 branches.
        from datetime import datetime as _dt

        self._fact(
            store,
            fid="f-start",
            text="event Mar 15",
            occurred_start=_dt(2026, 3, 15),
        )
        hits = await store.search_facts_temporal("b1", window)
        assert [h.fact_id for h in hits] == ["f-start"]

    async def test_fact_with_no_dates_excluded(
        self,
        store: InMemoryPageIndexStore,
        window,
    ) -> None:
        # Without any of the 3 date columns populated, nothing to match.
        self._fact(store, fid="f-no-date", text="undateable factoid")
        hits = await store.search_facts_temporal("b1", window)
        assert hits == []

    async def test_results_sorted_newest_first_via_coalesce(
        self,
        store: InMemoryPageIndexStore,
        window,
    ) -> None:
        # Hindsight order: COALESCE(occurred_start, mentioned_at,
        # occurred_end) DESC NULLS LAST. Top-k cap then keeps newest.
        from datetime import datetime as _dt

        self._fact(
            store,
            fid="oldest",
            text="early",
            occurred_start=_dt(2026, 3, 11),
        )
        self._fact(
            store,
            fid="middle",
            text="mid",
            mentioned_at=_dt(2026, 3, 15),
        )
        self._fact(
            store,
            fid="newest",
            text="late",
            occurred_end=_dt(2026, 3, 19),
        )
        hits = await store.search_facts_temporal("b1", window)
        assert [h.fact_id for h in hits] == ["newest", "middle", "oldest"]


class TestBenchHarnessConversion:
    """Pin the bench harness's tree ⇄ sections round-trip. The picker
    prompt was tuned against the nested-dict shape Phase A produced; the
    PR1 commit D port must reconstruct the same shape from flat
    ``PageIndexSection`` rows."""

    def test_flatten_walks_full_tree(self, bench_module) -> None:
        # 2 root nodes, 3 children under one of them. Verifies depth +
        # parent_node propagation and that the title-page line=1 isn't
        # special-cased (it's just another node).
        tree = {
            "structure": [
                {
                    "title": "Conversation X",
                    "node_id": "0000",
                    "line_num": 1,
                    "prefix_summary": "top",
                    "nodes": [
                        {
                            "title": "Session 1 (8 May, 2023)",
                            "node_id": "0001",
                            "line_num": 5,
                            "summary": "s1",
                            "date": "8 May, 2023",
                        },
                        {
                            "title": "Session 2 (9 May, 2023)",
                            "node_id": "0002",
                            "line_num": 50,
                            "summary": "s2",
                            "date": "9 May, 2023",
                        },
                    ],
                },
                {
                    "title": "Appendix",
                    "node_id": "0003",
                    "line_num": 100,
                    "summary": "ap",
                },
            ],
        }
        sections = bench_module._flatten_tree_to_sections(tree, "doc-uuid")
        line_nums = sorted(s.line_num for s in sections)
        assert line_nums == [1, 5, 50, 100]

        by_line = {s.line_num: s for s in sections}
        # Depth: roots are 0; children of root are 1.
        assert by_line[1].depth == 0
        assert by_line[100].depth == 0
        assert by_line[5].depth == 1
        assert by_line[50].depth == 1
        # parent_node propagated.
        assert by_line[5].parent_node == "0000"
        assert by_line[50].parent_node == "0000"
        assert by_line[1].parent_node is None
        # Date parsed.
        assert by_line[5].session_date is not None
        assert by_line[5].session_date.year == 2023
        assert by_line[5].session_date.month == 5
        assert by_line[5].session_date.day == 8
        # Summary preserved (root has prefix_summary, children have summary).
        assert by_line[1].summary == "top"
        assert by_line[5].summary == "s1"

    def test_flatten_skips_nodes_without_line_num(self, bench_module) -> None:
        # Stray nodes without an integer line_num can't be addressed by
        # the picker; flatten must drop them rather than write a bad row.
        tree = {
            "structure": [
                {"title": "ok", "node_id": "0000", "line_num": 1},
                {"title": "no line", "node_id": "0001"},
                {"title": "string line", "node_id": "0002", "line_num": "NaN"},
            ],
        }
        sections = bench_module._flatten_tree_to_sections(tree, "d")
        assert [s.line_num for s in sections] == [1]

    def test_reconstruct_inverse_of_flatten(self, bench_module) -> None:
        # The picker's "compact tree" shape must be byte-equivalent (mod
        # field ordering) after flatten → sections → reconstruct. This is
        # the contract that lets the store backend swap in without
        # touching the picker prompt.
        original = [
            {
                "title": "Conversation",
                "node_id": "0000",
                "line_num": 1,
                "summary": "top",
                "nodes": [
                    {
                        "title": "Session 1 (8 May, 2023)",
                        "node_id": "0001",
                        "line_num": 5,
                        "summary": "s1",
                        "date": "8 May, 2023",
                    },
                    {
                        "title": "Session 2 (9 May, 2023)",
                        "node_id": "0002",
                        "line_num": 50,
                        "summary": "s2",
                        "date": "9 May, 2023",
                    },
                ],
            },
        ]
        sections = bench_module._flatten_tree_to_sections(
            {"structure": original},
            "d",
        )
        rebuilt = bench_module._sections_to_compact_tree(sections)

        assert len(rebuilt) == 1
        root = rebuilt[0]
        assert root["title"] == "Conversation"
        assert root["node_id"] == "0000"
        assert root["line_num"] == 1
        assert root["summary"] == "top"
        assert "nodes" in root
        assert len(root["nodes"]) == 2
        # Children sorted by line_num.
        assert root["nodes"][0]["line_num"] == 5
        assert root["nodes"][0]["date"] == "8 May, 2023"
        assert root["nodes"][1]["line_num"] == 50
        assert root["nodes"][1]["date"] == "9 May, 2023"

    def test_reconstruct_orders_children_by_line_num(self, bench_module) -> None:
        # Sections may come out of the store in any order (SQL guarantees
        # ORDER BY line_num for skeleton, but defensive: reconstruct must
        # not depend on input order).
        sections = [
            _section("d", 50, parent_node="root", depth=1),
            _section("d", 5, parent_node="root", depth=1),
            _section("d", 1, parent_node=None, depth=0, node_id="root"),
            _section("d", 12, parent_node="root", depth=1),
        ]
        rebuilt = bench_module._sections_to_compact_tree(sections)
        assert len(rebuilt) == 1
        root = rebuilt[0]
        assert root["line_num"] == 1
        assert [n["line_num"] for n in root["nodes"]] == [5, 12, 50]

    def test_date_parse_round_trip(self, bench_module) -> None:
        # The synth's <reference_date> block uses the human-readable
        # form. Round-trip through datetime must preserve it.
        for raw in ["8 May, 2023", "1 January, 2024", "31 December, 2025"]:
            dt = bench_module._parse_session_date(raw)
            assert dt is not None
            assert bench_module._format_session_date(dt) == raw

    def test_date_parse_handles_missing(self, bench_module) -> None:
        assert bench_module._parse_session_date(None) is None
        assert bench_module._parse_session_date("") is None
        assert bench_module._parse_session_date("not a date") is None
        assert bench_module._format_session_date(None) is None

    # M33-3a — bare-ISO heading regex coverage
    def test_extract_date_from_title_locomo_form(self, bench_module) -> None:
        title = "(1:56 pm on 8 May, 2023)"
        assert bench_module._extract_date_from_title(title) == "8 May, 2023"

    def test_extract_date_from_title_lme_form(self, bench_module) -> None:
        title = "Session 4 (2023/05/20 (Sat) 02:21)"
        assert bench_module._extract_date_from_title(title) == "20 May, 2023"

    def test_extract_date_from_title_mem0_harness_form(self, bench_module) -> None:
        # M33-3a — the actual format LME sections use in mem0_harness:
        # ``Session N (YYYY-MM-DD HH:MM)``. Live-DB inspection on
        # v015i-aborted showed 100% of LME sections looked like
        # "Session 1 (2023-05-20 00:48)" and 0% were being matched
        # by the pre-fix regex set, leaving session_date NULL on
        # every fact downstream.
        title = "Session 1 (2023-05-20 00:48)"
        assert bench_module._extract_date_from_title(title) == "20 May, 2023"

    def test_extract_date_from_title_mem0_harness_form_no_time(self, bench_module) -> None:
        # Same shape without the trailing HH:MM (e.g. when the
        # timestamp was just a date).
        title = "Session 2 (2023-08-16)"
        assert bench_module._extract_date_from_title(title) == "16 August, 2023"

    def test_extract_date_from_title_no_match_returns_none(self, bench_module) -> None:
        for title in [
            "Conversation longmemeval_18bc8abd_1dd7eef8",  # top-level w/ no date
            "## Random Heading",
            "8 May, 2023",  # bare without parens — LoCoMo regex requires them
            "",
            None,
        ]:
            assert bench_module._extract_date_from_title(title) is None

    def test_enrich_nodes_with_dates_populates_date_field(self, bench_module) -> None:
        # Mem0-harness shape: ``Session N (YYYY-MM-DD HH:MM)``.
        tree = [
            {
                "title": "Session 1 (2023-08-16 09:48)",
                "line_num": 3,
                "node_id": "n1",
            },
            {
                "title": "Conversation longmemeval_xyz_abc",  # no date
                "line_num": 5,
                "node_id": "n2",
            },
        ]
        pairs = bench_module._enrich_nodes_with_dates(tree)
        # First node should be enriched; second should not.
        assert tree[0]["date"] == "16 August, 2023"
        assert "date" not in tree[1]
        assert pairs == [(3, "16 August, 2023")]

    def test_enrich_nodes_with_dates_unwraps_structure_dict(self, bench_module) -> None:
        # M33-3a regression — _document_tree_to_pageindex_dict in the
        # mem0_harness path returns ``{"structure": [...]}`` rather
        # than a bare list. The enrich walker has to handle BOTH or
        # it silently no-ops on the entire bench, leaving session_date
        # NULL on every fact (the v015i-run-1 failure mode).
        wrapped = {
            "structure": [
                {
                    "title": "Session 1 (2023-05-20 00:48)",
                    "line_num": 3,
                    "node_id": "n1",
                    "nodes": [
                        {
                            "title": "Session 2 (2023-05-21 12:55)",
                            "line_num": 7,
                            "node_id": "n2",
                        },
                    ],
                },
            ],
        }
        pairs = bench_module._enrich_nodes_with_dates(wrapped)
        # Both root and child should be enriched.
        assert wrapped["structure"][0]["date"] == "20 May, 2023"
        assert wrapped["structure"][0]["nodes"][0]["date"] == "21 May, 2023"
        # Order follows the depth-first walk.
        assert pairs == [(3, "20 May, 2023"), (7, "21 May, 2023")]


# ── Tests: end-to-end build_or_load_tree via in-memory store ──────────


class TestBuildOrLoadTreeWithStore:
    """End-to-end through the bench's ``build_or_load_tree`` against the
    in-memory store. Cache-hit must reproduce the same picker-facing
    shape as a fresh build — that's the regression contract."""

    @pytest.fixture(scope="class")
    def bench_module(self):
        return _import_bench_module()

    @pytest.fixture
    def synthetic_conv(self) -> dict:
        """A minimal LoCoMo-shaped conversation that exercises both
        sessions (with dates) and per-session turns. Real LoCoMo convs
        are larger but the shape is what matters for the conversion."""
        return {
            "sample_id": "synth-1",
            "conversation": {
                "speaker_a": "Alice",
                "speaker_b": "Bob",
                "session_1": [
                    {"speaker": "Alice", "text": "Hello.", "dia_id": "D1:1"},
                    {"speaker": "Bob", "text": "Hi.", "dia_id": "D1:2"},
                ],
                "session_1_date_time": "1:00 pm on 8 May, 2023",
                "session_2": [
                    {"speaker": "Alice", "text": "Still here.", "dia_id": "D2:1"},
                ],
                "session_2_date_time": "2:00 pm on 9 May, 2023",
            },
        }

    async def test_save_then_load_returns_equivalent_shape(
        self,
        bench_module,
        synthetic_conv: dict,
        tmp_path: Path,
    ) -> None:
        # Use the in-memory store's save/load directly to avoid the
        # md_to_tree LLM call that build_or_load_tree triggers on a
        # cache miss. We assert the conversion helpers produce a
        # round-trippable shape; the LLM-call path is exercised by the
        # bench gate, not by unit tests.
        store = InMemoryPageIndexStore()

        # Manually construct a tree as md_to_tree would after enrichment.
        fake_tree = {
            "structure": [
                {
                    "title": "Conversation synth-1",
                    "node_id": "0000",
                    "line_num": 1,
                    "prefix_summary": "top",
                    "nodes": [
                        {
                            "title": "Session 1 (1:00 pm on 8 May, 2023)",
                            "node_id": "0001",
                            "line_num": 5,
                            "summary": "s1",
                            "date": "8 May, 2023",
                        },
                        {
                            "title": "Session 2 (2:00 pm on 9 May, 2023)",
                            "node_id": "0002",
                            "line_num": 30,
                            "summary": "s2",
                            "date": "9 May, 2023",
                        },
                    ],
                },
            ],
        }
        # Save via the store
        doc = PageIndexDocument(
            id="",
            bank_id="test-bank",
            source_id="synth-1",
            md_text="md body",
            reference_date=datetime(2023, 5, 9, tzinfo=timezone.utc),
            built_at=datetime.now(tz=timezone.utc),
        )
        doc_id = await store.save_document(doc)
        sections = bench_module._flatten_tree_to_sections(fake_tree, doc_id)
        await store.save_sections(doc_id, sections)

        # Load via the store and reconstruct the picker shape
        loaded_doc = await store.load_document("test-bank", "synth-1")
        assert loaded_doc is not None
        loaded_sections = await store.load_skeleton(loaded_doc.id)
        rebuilt = bench_module._sections_to_compact_tree(loaded_sections)

        # Compare line_nums (the picker's primary addressing axis)
        def collect_line_nums(items):
            out = []
            if not isinstance(items, list):
                return out
            for n in items:
                out.append(n["line_num"])
                if n.get("nodes"):
                    out.extend(collect_line_nums(n["nodes"]))
            return out

        assert collect_line_nums(fake_tree["structure"]) == collect_line_nums(rebuilt)
        assert loaded_doc.md_text == "md body"


# ── Tests: list_distinct_entities (PR2.6 / agentic counting tool) ──────


class TestInMemoryListDistinctEntities:
    """``PageIndexStore.list_distinct_entities`` SPI conformance.

    Pinned behaviours (mirror the Postgres adapter's ILIKE semantics):
    - ``pattern=None`` → top-N entities by mention count (count desc, name asc)
    - ``pattern="alice"`` → case-insensitive substring match
    - ``pattern="%kit%"`` → ``%`` is stripped (the field is a substring,
      not a SQL wildcard at the SPI surface — Postgres handles ILIKE
      wildcards on its side; the in-memory adapter mirrors the same
      shape so callers see consistent results across both backends)
    - distinct ``(line_num, entity_name)`` collapses duplicate mentions
    - empty document → empty result
    - ``limit`` caps output
    - cross-document isolation: only entities for ``document_id`` count
    """

    @pytest.fixture
    async def populated(self):
        store = InMemoryPageIndexStore()
        doc1 = await store.save_document(
            PageIndexDocument(
                id="",
                bank_id="b1",
                source_id="conv-1",
                md_text="x",
                reference_date=None,
                built_at=datetime.now(tz=timezone.utc),
            )
        )
        doc2 = await store.save_document(
            PageIndexDocument(
                id="",
                bank_id="b1",
                source_id="conv-2",
                md_text="x",
                reference_date=None,
                built_at=datetime.now(tz=timezone.utc),
            )
        )
        # Sections must exist before save_section_entities (FK to (doc, line)).
        for doc_id in (doc1, doc2):
            await store.save_sections(
                doc_id,
                [
                    PageIndexSection(
                        document_id=doc_id,
                        line_num=ln,
                        node_id=f"{ln:04d}",
                        title=f"S{ln}",
                        depth=1,
                    )
                    for ln in (1, 2, 3)
                ],
            )
        # Doc 1: Alice ×3, Bob ×2, Carol ×1.
        await store.save_section_entities(
            [
                PageIndexSectionEntity(document_id=doc1, line_num=1, entity_name="Alice"),
                PageIndexSectionEntity(document_id=doc1, line_num=2, entity_name="Alice"),
                PageIndexSectionEntity(document_id=doc1, line_num=3, entity_name="Alice"),
                PageIndexSectionEntity(document_id=doc1, line_num=1, entity_name="Bob"),
                PageIndexSectionEntity(document_id=doc1, line_num=2, entity_name="Bob"),
                PageIndexSectionEntity(document_id=doc1, line_num=1, entity_name="Carol"),
            ]
        )
        # Doc 2: Dave ×1 (must NOT leak into doc 1's results).
        await store.save_section_entities(
            [
                PageIndexSectionEntity(document_id=doc2, line_num=1, entity_name="Dave"),
            ]
        )
        return store, doc1, doc2

    @pytest.mark.asyncio
    async def test_no_pattern_returns_top_by_count_desc(self, populated):
        store, doc1, _ = populated
        result = await store.list_distinct_entities("b1", doc1)
        # Count desc: Alice (3), Bob (2), Carol (1).
        assert result == [("Alice", 3), ("Bob", 2), ("Carol", 1)]

    @pytest.mark.asyncio
    async def test_pattern_filter_case_insensitive(self, populated):
        store, doc1, _ = populated
        # "ALI" matches "Alice" regardless of case.
        result_upper = await store.list_distinct_entities("b1", doc1, pattern="ALI")
        result_lower = await store.list_distinct_entities("b1", doc1, pattern="ali")
        assert result_upper == [("Alice", 3)]
        assert result_lower == [("Alice", 3)]

    @pytest.mark.asyncio
    async def test_percent_wildcards_stripped_to_substring(self, populated):
        """``%bo%`` is treated as substring 'bo' — % is stripped to
        match the Postgres ILIKE shape at the in-memory layer."""
        store, doc1, _ = populated
        result = await store.list_distinct_entities("b1", doc1, pattern="%bo%")
        assert result == [("Bob", 2)]

    @pytest.mark.asyncio
    async def test_distinct_per_section_collapses_duplicates(self, populated):
        """If the same (line_num, entity_name) is inserted twice, the
        count reflects DISTINCT sections — matches the Postgres
        composite PK (document_id, line_num, entity_name)."""
        store, doc1, _ = populated
        # Re-insert Alice@line=1; must NOT bump the count.
        await store.save_section_entities(
            [
                PageIndexSectionEntity(document_id=doc1, line_num=1, entity_name="Alice"),
            ]
        )
        result = await store.list_distinct_entities("b1", doc1, pattern="alice")
        assert result == [("Alice", 3)]  # still 3, not 4

    @pytest.mark.asyncio
    async def test_cross_document_isolation(self, populated):
        """Entities in doc2 must not appear in doc1's results."""
        store, doc1, doc2 = populated
        result_doc1 = await store.list_distinct_entities("b1", doc1)
        assert "Dave" not in {name for name, _ in result_doc1}
        # Reverse direction: doc2 should ONLY contain Dave.
        result_doc2 = await store.list_distinct_entities("b1", doc2)
        assert result_doc2 == [("Dave", 1)]

    @pytest.mark.asyncio
    async def test_limit_caps_output(self, populated):
        store, doc1, _ = populated
        result = await store.list_distinct_entities("b1", doc1, limit=2)
        # Keeps the top 2 by count.
        assert [name for name, _ in result] == ["Alice", "Bob"]

    @pytest.mark.asyncio
    async def test_empty_document_returns_empty(self):
        store = InMemoryPageIndexStore()
        empty_doc = await store.save_document(
            PageIndexDocument(
                id="",
                bank_id="b1",
                source_id="empty",
                md_text="",
                reference_date=None,
                built_at=datetime.now(tz=timezone.utc),
            )
        )
        result = await store.list_distinct_entities("b1", empty_doc)
        assert result == []

    @pytest.mark.asyncio
    async def test_no_match_returns_empty(self, populated):
        store, doc1, _ = populated
        result = await store.list_distinct_entities("b1", doc1, pattern="zzz-no-such")
        assert result == []
