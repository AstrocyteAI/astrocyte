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
        self, store: InMemoryPageIndexStore,
    ) -> None:
        # ``None`` (not exception) is the contract — caller decides
        # whether to build the doc.
        assert await store.load_document("b1", "no-such-conv") is None

    async def test_upsert_keyed_on_bank_id_source_id(
        self, store: InMemoryPageIndexStore,
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
        self, store: InMemoryPageIndexStore,
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
        self, store: InMemoryPageIndexStore,
    ) -> None:
        # Mirrors the FK ON DELETE CASCADE in migration 015 — atomic
        # tree replace must drop dependent rows, not orphan them.
        doc_id = await store.save_document(_doc())
        await store.save_sections(doc_id, [_section(doc_id, 1), _section(doc_id, 5)])
        await store.save_section_entities([
            PageIndexSectionEntity(document_id=doc_id, line_num=1, entity_name="Alice"),
        ])
        await store.save_section_links([
            PageIndexSectionLink(
                from_doc=doc_id, from_line=1,
                to_doc=doc_id, to_line=5,
                link_type="semantic_knn", weight=0.9,
            ),
        ])
        # Replace the tree.
        await store.save_sections(doc_id, [_section(doc_id, 99)])
        # Old entity/link buckets must be empty (cascade).
        assert store._section_entities.get(doc_id) is None
        assert store._section_links.get(doc_id) is None

    async def test_load_skeleton_orders_by_line_num(
        self, store: InMemoryPageIndexStore,
    ) -> None:
        doc_id = await store.save_document(_doc())
        # Insert in scrambled order — load must sort.
        await store.save_sections(doc_id, [
            _section(doc_id, 50),
            _section(doc_id, 1),
            _section(doc_id, 12),
        ])
        skel = await store.load_skeleton(doc_id)
        assert [s.line_num for s in skel] == [1, 12, 50]

    async def test_load_skeleton_strips_summary_embedding(
        self, store: InMemoryPageIndexStore,
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
        self, store: InMemoryPageIndexStore,
    ) -> None:
        doc_id = await store.save_document(_doc())
        await store.save_sections(doc_id, [_section(doc_id, 1)])
        e = PageIndexSectionEntity(document_id=doc_id, line_num=1, entity_name="Alice")
        n1 = await store.save_section_entities([e])
        n2 = await store.save_section_entities([e])  # same row
        assert n1 == 1 and n2 == 0, "second insert is a no-op on the composite PK"

    async def test_save_section_links_idempotent(
        self, store: InMemoryPageIndexStore,
    ) -> None:
        doc_id = await store.save_document(_doc())
        await store.save_sections(doc_id, [_section(doc_id, 1), _section(doc_id, 5)])
        link = PageIndexSectionLink(
            from_doc=doc_id, from_line=1, to_doc=doc_id, to_line=5,
            link_type="semantic_knn", weight=0.85,
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

    bench_path = (
        Path(__file__).resolve().parent.parent / "scripts" / "bench_pageindex_locomo.py"
    )
    spec = importlib.util.spec_from_file_location("bench_pi_locomo", bench_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bench_module():
    return _import_bench_module()


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
            {"structure": original}, "d",
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
        self, bench_module, synthetic_conv: dict, tmp_path: Path,
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
