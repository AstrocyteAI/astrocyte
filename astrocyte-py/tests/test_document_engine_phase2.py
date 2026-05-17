"""M17 Phase 2 tests — summarizer + in-memory storage.

Postgres-impl tests live in adapters-storage-py/ and require a live DB;
skipped from this suite.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from astrocyte.documents import (
    AdaptiveSummarizer,
    Document,
    DocumentTree,
    InMemoryDocumentStore,
    TreeNode,
    build_markdown_tree,
)

# ─── fake LLM call for testing ────────────────────────────────────────


def make_fake_llm(
    *,
    canned: str = "(LLM-generated description)",
    record_to: list | None = None,
) -> Any:
    """Build an LLM call that records every prompt + returns canned text."""

    async def fake_llm(prompt: str) -> str:
        if record_to is not None:
            record_to.append(prompt)
        return canned

    return fake_llm


# ─── AdaptiveSummarizer ───────────────────────────────────────────────


class TestSummarizer:
    @pytest.mark.asyncio
    async def test_small_node_uses_raw_text(self) -> None:
        """Node text < threshold → summary IS the text, no LLM call."""
        calls: list[str] = []
        summarizer = AdaptiveSummarizer(make_fake_llm(record_to=calls))

        n = TreeNode.new(parent_id=None, depth=1, title="A", text="short text")
        summary = await summarizer.summarize_node(n)

        assert summary.kind == "raw"
        assert summary.text == "short text"
        assert n.summary is summary
        assert calls == []  # no LLM call

    @pytest.mark.asyncio
    async def test_large_node_triggers_llm(self) -> None:
        """Node text ≥ threshold → LLM call → kind='llm'."""
        big_text = "a long word " * 200  # ~600 tokens easily
        calls: list[str] = []
        summarizer = AdaptiveSummarizer(make_fake_llm(record_to=calls))

        n = TreeNode.new(parent_id=None, depth=1, title="A", text=big_text)
        summary = await summarizer.summarize_node(n)

        assert summary.kind == "llm"
        assert summary.text == "(LLM-generated description)"
        assert len(calls) == 1
        assert "a long word" in calls[0]  # PageIndex prompt embeds the text

    @pytest.mark.asyncio
    async def test_custom_threshold(self) -> None:
        """Threshold override changes the gate."""
        summarizer = AdaptiveSummarizer(
            make_fake_llm(canned="X"),
            threshold_tokens=5,
        )
        # ~7 words → roughly 9 tokens via heuristic → > 5 → LLM
        n = TreeNode.new(
            parent_id=None,
            depth=1,
            title="A",
            text="this has multiple words in it now please",
        )
        await summarizer.summarize_node(n)
        assert n.summary is not None
        assert n.summary.kind == "llm"

    @pytest.mark.asyncio
    async def test_threshold_validation(self) -> None:
        with pytest.raises(ValueError):
            AdaptiveSummarizer(make_fake_llm(), threshold_tokens=0)

    @pytest.mark.asyncio
    async def test_llm_failure_degrades_to_raw(self) -> None:
        """LLM raising → summary falls back to raw node text."""

        async def bad_llm(prompt: str) -> str:
            raise RuntimeError("LLM down")

        summarizer = AdaptiveSummarizer(bad_llm, threshold_tokens=5)
        n = TreeNode.new(
            parent_id=None,
            depth=1,
            title="A",
            text="long enough text to trigger the LLM path here",
        )
        summary = await summarizer.summarize_node(n)
        assert summary.kind == "raw"
        assert "long enough text" in summary.text

    @pytest.mark.asyncio
    async def test_empty_llm_response_falls_back_to_raw(self) -> None:
        """Empty LLM response → use raw text instead of empty summary."""

        async def empty_llm(prompt: str) -> str:
            return ""

        summarizer = AdaptiveSummarizer(empty_llm, threshold_tokens=5)
        n = TreeNode.new(
            parent_id=None,
            depth=1,
            title="A",
            text="long enough text to trigger the LLM path here",
        )
        summary = await summarizer.summarize_node(n)
        assert summary.text != ""

    @pytest.mark.asyncio
    async def test_summarize_tree_marks_internal_as_prefix(self) -> None:
        """After tree walk, internal nodes get kind='prefix'; leaves keep raw/llm."""
        md = "# Top\n\nintro\n\n## Sub\n\nleaf body"
        tree = build_markdown_tree(md, "doc1")

        summarizer = AdaptiveSummarizer(make_fake_llm())
        await summarizer.summarize_tree(tree)

        top = tree.roots[0]
        sub = top.children[0]
        # Internal node (Top) → prefix
        assert top.summary is not None
        assert top.summary.kind == "prefix"
        # Leaf node (Sub) → raw (short text)
        assert sub.summary is not None
        assert sub.summary.kind == "raw"
        assert sub.summary.text.startswith("## Sub")

    @pytest.mark.asyncio
    async def test_summarize_tree_empty(self) -> None:
        """Empty tree → no error."""
        tree = DocumentTree(document_id="doc1", roots=[])
        await AdaptiveSummarizer(make_fake_llm()).summarize_tree(tree)
        # Just doesn't raise.

    @pytest.mark.asyncio
    async def test_summarize_tree_runs_concurrently(self) -> None:
        """Verify the gather() shape — all nodes processed."""
        md = "# A\n## B\n## C\n## D"
        tree = build_markdown_tree(md, "doc1")
        await AdaptiveSummarizer(make_fake_llm()).summarize_tree(tree)
        for n in tree.all_nodes():
            assert n.summary is not None


# ─── InMemoryDocumentStore ────────────────────────────────────────────


class TestInMemoryStore:
    @pytest.mark.asyncio
    async def test_save_and_get_document(self) -> None:
        store = InMemoryDocumentStore()
        doc = Document.new(source_uri="inline://x", content="hello")
        await store.save_document(doc)
        loaded = await store.get_document(doc.id)
        assert loaded is not None
        assert loaded.id == doc.id
        assert loaded.source_uri == "inline://x"
        assert loaded.tree is None  # never inlined on metadata read

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self) -> None:
        store = InMemoryDocumentStore()
        assert await store.get_document("nope") is None
        assert await store.get_tree("nope") is None

    @pytest.mark.asyncio
    async def test_save_with_tree(self) -> None:
        store = InMemoryDocumentStore()
        doc = Document.new(content="x")
        md = "# Top\n\nintro\n\n## Sub\n\nbody"
        tree = build_markdown_tree(md, doc.id)
        await store.save_document(doc, tree=tree)

        loaded_tree = await store.get_tree(doc.id)
        assert loaded_tree is not None
        assert loaded_tree.node_count() == 2
        assert loaded_tree.roots[0].title == "Top"

    @pytest.mark.asyncio
    async def test_save_replaces_existing_tree(self) -> None:
        store = InMemoryDocumentStore()
        doc = Document.new(content="x")
        t1 = build_markdown_tree("# Old", doc.id)
        await store.save_document(doc, tree=t1)
        t2 = build_markdown_tree("# New\n## Subnew", doc.id)
        await store.save_document(doc, tree=t2)
        loaded = await store.get_tree(doc.id)
        assert loaded is not None
        assert loaded.node_count() == 2
        assert loaded.roots[0].title == "New"

    @pytest.mark.asyncio
    async def test_list_documents_newest_first(self) -> None:
        store = InMemoryDocumentStore()
        d1 = Document.new(source_uri="a")
        await store.save_document(d1)
        await asyncio.sleep(0.001)
        d2 = Document.new(source_uri="b")
        await store.save_document(d2)
        listed = await store.list_documents()
        assert len(listed) == 2
        assert listed[0].source_uri == "b"  # newest first
        assert listed[1].source_uri == "a"

    @pytest.mark.asyncio
    async def test_list_respects_limit(self) -> None:
        store = InMemoryDocumentStore()
        for _ in range(5):
            await store.save_document(Document.new(content="x"))
        assert len(await store.list_documents(limit=3)) == 3

    @pytest.mark.asyncio
    async def test_delete_removes_doc_and_tree(self) -> None:
        store = InMemoryDocumentStore()
        doc = Document.new(content="x")
        tree = build_markdown_tree("# T", doc.id)
        await store.save_document(doc, tree=tree)
        await store.delete_document(doc.id)
        assert await store.get_document(doc.id) is None
        assert await store.get_tree(doc.id) is None

    @pytest.mark.asyncio
    async def test_delete_missing_is_noop(self) -> None:
        store = InMemoryDocumentStore()
        await store.delete_document("nope")  # must not raise

    @pytest.mark.asyncio
    async def test_save_idempotent(self) -> None:
        """Save same document twice → no duplicate, second wins."""
        store = InMemoryDocumentStore()
        doc = Document.new(source_uri="a", content="x")
        await store.save_document(doc)
        doc.title = "Updated"
        await store.save_document(doc)
        loaded = await store.get_document(doc.id)
        assert loaded is not None
        assert loaded.title == "Updated"
        assert len(await store.list_documents()) == 1


# ─── end-to-end smoke (Phase 1 + Phase 2 integration) ────────────────


class TestPhase2EndToEnd:
    @pytest.mark.asyncio
    async def test_parse_build_summarize_store_reload(self) -> None:
        """The full Phase 2 happy path, no LLM cost (fake summarizer)."""
        md = """# Astrocyte M17

The Document Engine ships in Phase 2.

## Architecture

Two engines, composable.

## Storage

DocumentStore SPI + InMemoryDocumentStore + Postgres impl.
"""
        # 1. Parse / build (Phase 1)
        doc = Document.new(source_uri="inline://m17", content=md, title="M17")
        tree = build_markdown_tree(md, doc.id)
        assert tree.node_count() == 3

        # 2. Summarize (Phase 2)
        await AdaptiveSummarizer(make_fake_llm()).summarize_tree(tree)
        assert all(n.summary is not None for n in tree.all_nodes())

        # 3. Store (Phase 2)
        store = InMemoryDocumentStore()
        await store.save_document(doc, tree=tree)

        # 4. Reload and verify shape preserved
        loaded_doc = await store.get_document(doc.id)
        loaded_tree = await store.get_tree(doc.id)
        assert loaded_doc is not None
        assert loaded_doc.title == "M17"
        assert loaded_tree is not None
        assert loaded_tree.node_count() == 3
        assert loaded_tree.roots[0].title == "Astrocyte M17"
