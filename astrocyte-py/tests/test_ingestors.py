"""M17 Phase 3 tests — Document + Conversation ingestors.

Verifies the composition layer:
  - DocumentIngestor walks a tree and emits one retain() per node
  - ConversationIngestor chunks + emits one retain() per chunk
  - Metadata shape carries source-attribution + opaque cross-refs
  - Per-segment failures are swallowed (one bad node/chunk doesn't kill ingest)
  - The Memory Engine SPI is just an async callable — testable without a real DB
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from astrocyte.conversations import (
    Conversation,
    ConversationIngestor,
)
from astrocyte.documents import (
    AdaptiveSummarizer,
    Document,
    DocumentIngestor,
    build_markdown_tree,
)

# ─── helpers: in-memory retain spy ────────────────────────────────────


class RetainSpy:
    """Records every retain call. Acts as the Memory Engine in tests."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        *,
        bank_id: str,
        content: str,
        metadata: dict[str, Any],
    ) -> None:
        self.calls.append(
            {
                "bank_id": bank_id,
                "content": content,
                "metadata": metadata,
            }
        )


def fake_failing_retain(fail_on_indices: set[int]) -> Any:
    """Build a retain that raises on specific call-index positions."""
    counter = [0]

    async def retain(*, bank_id: str, content: str, metadata: dict[str, Any]) -> None:
        i = counter[0]
        counter[0] += 1
        if i in fail_on_indices:
            raise RuntimeError(f"injected failure at index {i}")

    return retain


# ─── DocumentIngestor ─────────────────────────────────────────────────


class TestDocumentIngestor:
    @pytest.mark.asyncio
    async def test_ingests_one_call_per_node(self) -> None:
        spy = RetainSpy()
        ingestor = DocumentIngestor(retain=spy)
        doc = Document.new(source_uri="inline://x", content="x")
        md = "# Top\n\nintro\n\n## Sub A\n\nA body\n\n## Sub B\n\nB body"
        tree = build_markdown_tree(md, doc.id)
        assert tree.node_count() == 3

        result = await ingestor.ingest(tree, doc, bank_id="bank-1")

        assert result.segments_emitted == 3
        assert len(spy.calls) == 3
        assert result.ok
        assert result.source_kind == "astrocyte.documents"
        assert result.source_id == doc.id
        assert result.bank_id == "bank-1"

    @pytest.mark.asyncio
    async def test_metadata_shape(self) -> None:
        spy = RetainSpy()
        ingestor = DocumentIngestor(retain=spy)
        doc = Document.new(source_uri="inline://test", content="x", title="Test Doc")
        tree = build_markdown_tree("# Top\n\nbody", doc.id)
        await ingestor.ingest(tree, doc, bank_id="bank-1")

        m = spy.calls[0]["metadata"]
        assert m["source"] == "astrocyte.documents"
        assert m["source_document_id"] == doc.id
        assert m["source_uri"] == "inline://test"
        assert "tree_node_id" in m
        assert "tree_node_depth" in m
        assert "tree_node_title" in m
        assert m["tree_node_title"] == "Top"

    @pytest.mark.asyncio
    async def test_per_node_failure_swallowed(self) -> None:
        # Three nodes; fail on index 1 → other two still emit
        retain = fake_failing_retain({1})
        ingestor = DocumentIngestor(retain=retain)
        doc = Document.new(content="x")
        tree = build_markdown_tree("# A\n## B\n## C", doc.id)
        result = await ingestor.ingest(tree, doc, bank_id="b1")
        assert result.segments_emitted == 2
        assert len(result.failures) == 1
        assert not result.ok

    @pytest.mark.asyncio
    async def test_skip_empty_text(self) -> None:
        spy = RetainSpy()
        ingestor = DocumentIngestor(retain=spy, skip_empty_text=True)
        # Empty tree → no calls
        doc = Document.new(content="")
        from astrocyte.documents.types import DocumentTree

        tree = DocumentTree(document_id=doc.id, roots=[])
        result = await ingestor.ingest(tree, doc, bank_id="b1")
        assert result.segments_emitted == 0

    @pytest.mark.asyncio
    async def test_extra_metadata_propagates(self) -> None:
        spy = RetainSpy()
        ingestor = DocumentIngestor(retain=spy)
        doc = Document.new(content="x")
        tree = build_markdown_tree("# Top", doc.id)
        await ingestor.ingest(
            tree,
            doc,
            bank_id="b1",
            extra_metadata={"ingest_session_id": "sess-42", "user_id": "u-99"},
        )
        m = spy.calls[0]["metadata"]
        assert m["ingest_session_id"] == "sess-42"
        assert m["user_id"] == "u-99"

    @pytest.mark.asyncio
    async def test_prefers_summary_for_large_nodes(self) -> None:
        """Nodes with text > prefer_summary_over_chars use the LLM summary."""

        async def fake_llm(prompt: str) -> str:
            return "(short summary)"

        big_text = "x " * 3000  # ~6000 chars
        doc = Document.new(content=big_text)
        # Build a single-node tree manually (headers absent → synthetic root)
        tree = build_markdown_tree(big_text, doc.id)
        # Run the summarizer to mark the node with kind='llm'
        await AdaptiveSummarizer(fake_llm, threshold_tokens=10).summarize_tree(tree)
        node = tree.roots[0]
        assert node.summary is not None
        # The single-root case is treated as leaf (no children), kind should be 'llm'
        assert node.summary.kind == "llm"

        spy = RetainSpy()
        ingestor = DocumentIngestor(retain=spy, prefer_summary_over_chars=4_000)
        await ingestor.ingest(tree, doc, bank_id="b1")
        # Should use the summary (small) instead of raw (huge)
        assert spy.calls[0]["content"] == "(short summary)"


# ─── ConversationIngestor ─────────────────────────────────────────────


class TestConversationIngestor:
    @pytest.mark.asyncio
    async def test_ingests_one_call_per_chunk(self) -> None:
        spy = RetainSpy()
        ingestor = ConversationIngestor(retain=spy, max_chars_per_chunk=200)
        c = Conversation.new(source_uri="bench://lme/q-test")
        for i in range(6):
            c.add_turn(
                role="user" if i % 2 == 0 else "assistant", content=f"turn {i} with some moderate-length content here"
            )
        result = await ingestor.ingest(c, bank_id="b1")
        # 6 turns at ~50 chars + render overhead → multi-chunk
        assert result.segments_emitted > 1
        assert len(spy.calls) == result.segments_emitted
        assert result.ok
        assert result.source_kind == "astrocyte.conversations"

    @pytest.mark.asyncio
    async def test_chunk_content_uses_role_markers(self) -> None:
        spy = RetainSpy()
        ingestor = ConversationIngestor(retain=spy)
        c = Conversation.new()
        c.add_turn(role="user", content="hi")
        c.add_turn(role="assistant", content="hello")
        await ingestor.ingest(c, bank_id="b1")
        content = spy.calls[0]["content"]
        assert "**user**: hi" in content
        assert "**assistant**: hello" in content

    @pytest.mark.asyncio
    async def test_metadata_shape(self) -> None:
        spy = RetainSpy()
        ingestor = ConversationIngestor(retain=spy)
        now = datetime.now(timezone.utc)
        c = Conversation.new(source_uri="slack://x", title="thread")
        c.add_turn(role="user", content="hi", timestamp=now)
        c.add_turn(role="assistant", content="hello", timestamp=now)
        await ingestor.ingest(c, bank_id="b1")
        m = spy.calls[0]["metadata"]
        assert m["source"] == "astrocyte.conversations"
        assert m["source_conversation_id"] == c.id
        assert m["source_uri"] == "slack://x"
        assert m["conversation_title"] == "thread"
        assert "chunk_index" in m
        assert "turn_count" in m
        assert "turn_ids" in m
        assert len(m["turn_ids"]) == 2
        assert m["earliest_timestamp"] is not None

    @pytest.mark.asyncio
    async def test_per_chunk_failure_swallowed(self) -> None:
        retain = fake_failing_retain({0})
        ingestor = ConversationIngestor(retain=retain, max_chars_per_chunk=100)
        c = Conversation.new()
        for i in range(6):
            c.add_turn(role="user", content="filler " * 5)
        result = await ingestor.ingest(c, bank_id="b1")
        # First chunk failed; remaining chunks succeeded
        assert len(result.failures) == 1
        assert result.segments_emitted > 0
        assert not result.ok

    @pytest.mark.asyncio
    async def test_empty_conversation_zero_emissions(self) -> None:
        spy = RetainSpy()
        ingestor = ConversationIngestor(retain=spy)
        c = Conversation.new()
        result = await ingestor.ingest(c, bank_id="b1")
        assert result.segments_emitted == 0
        assert spy.calls == []

    @pytest.mark.asyncio
    async def test_extra_metadata_propagates(self) -> None:
        spy = RetainSpy()
        ingestor = ConversationIngestor(retain=spy)
        c = Conversation.new()
        c.add_turn(role="user", content="x")
        await ingestor.ingest(c, bank_id="b1", extra_metadata={"trace_id": "abc-123"})
        assert spy.calls[0]["metadata"]["trace_id"] == "abc-123"
