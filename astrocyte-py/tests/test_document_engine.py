"""Tests for the Document Engine — types, parsers, md_builder.

Phase 1 of M17. No LLM, no storage; just structural correctness.
"""

from __future__ import annotations

import pytest

from astrocyte.documents import (
    ConvertResult,
    Document,
    DocumentTree,
    MarkdownParser,
    NodeSummary,
    Parser,
    ParserRegistry,
    TreeNode,
    UnsupportedFileTypeError,
    build_markdown_tree,
)

# ─── types: TreeNode ──────────────────────────────────────────────────


class TestTreeNode:
    def test_new_generates_uuid(self) -> None:
        n = TreeNode.new(parent_id=None, depth=1, title="root")
        assert len(n.id) == 36  # UUID4 length
        assert n.parent_id is None
        assert n.depth == 1
        assert n.title == "root"

    def test_new_rejects_bad_depth(self) -> None:
        with pytest.raises(ValueError, match="depth"):
            TreeNode.new(parent_id=None, depth=0, title="x")
        with pytest.raises(ValueError, match="depth"):
            TreeNode.new(parent_id=None, depth=7, title="x")

    def test_add_child_sets_parent_id(self) -> None:
        parent = TreeNode.new(parent_id=None, depth=1, title="p")
        child = TreeNode.new(parent_id="wrong-id", depth=2, title="c")
        parent.add_child(child)
        assert child.parent_id == parent.id
        assert parent.children == [child]

    def test_traverse_pre_order(self) -> None:
        root = TreeNode.new(parent_id=None, depth=1, title="root")
        c1 = TreeNode.new(parent_id=root.id, depth=2, title="c1")
        c2 = TreeNode.new(parent_id=root.id, depth=2, title="c2")
        gc = TreeNode.new(parent_id=c1.id, depth=3, title="gc")
        root.add_child(c1)
        root.add_child(c2)
        c1.add_child(gc)
        titles = [n.title for n in root.traverse_pre()]
        assert titles == ["root", "c1", "gc", "c2"]

    def test_is_leaf(self) -> None:
        root = TreeNode.new(parent_id=None, depth=1, title="x")
        assert root.is_leaf()
        root.add_child(TreeNode.new(parent_id=root.id, depth=2, title="y"))
        assert not root.is_leaf()


class TestNodeSummary:
    def test_default_kind_is_raw(self) -> None:
        s = NodeSummary(text="hello")
        assert s.kind == "raw"
        assert len(s) == 5


# ─── types: DocumentTree ──────────────────────────────────────────────


class TestDocumentTree:
    def test_all_nodes_walks_all_roots(self) -> None:
        a = TreeNode.new(parent_id=None, depth=1, title="A")
        b = TreeNode.new(parent_id=None, depth=1, title="B")
        a.add_child(TreeNode.new(parent_id=a.id, depth=2, title="A1"))
        tree = DocumentTree(document_id="doc1", roots=[a, b])
        assert tree.node_count() == 3
        assert [n.title for n in tree.all_nodes()] == ["A", "A1", "B"]

    def test_find_returns_node_or_none(self) -> None:
        a = TreeNode.new(parent_id=None, depth=1, title="A")
        tree = DocumentTree(document_id="doc1", roots=[a])
        assert tree.find(a.id) is a
        assert tree.find("nonexistent") is None


# ─── types: Document ──────────────────────────────────────────────────


class TestDocument:
    def test_new_computes_hash(self) -> None:
        d = Document.new(source_uri="inline://x", content="hello world")
        assert d.id
        assert d.content_hash
        assert len(d.content_hash) == 64  # SHA-256 hex
        assert d.source_uri == "inline://x"

    def test_same_content_same_hash(self) -> None:
        d1 = Document.new(content="same text")
        d2 = Document.new(content="same text")
        assert d1.content_hash == d2.content_hash
        assert d1.id != d2.id  # ids are still distinct

    def test_bytes_or_str_content(self) -> None:
        d1 = Document.new(content="abc")
        d2 = Document.new(content=b"abc")
        assert d1.content_hash == d2.content_hash


# ─── Parser ABC + MarkdownParser + Registry ───────────────────────────


class TestMarkdownParser:
    @pytest.mark.asyncio
    async def test_passes_text_through(self) -> None:
        p = MarkdownParser()
        out = await p.convert(b"# hi\n\nworld", "x.md")
        assert out == "# hi\n\nworld"

    @pytest.mark.asyncio
    async def test_utf8_decode(self) -> None:
        p = MarkdownParser()
        out = await p.convert("café".encode("utf-8"), "x.md")
        assert "café" in out

    @pytest.mark.asyncio
    async def test_malformed_utf8_doesnt_crash(self) -> None:
        p = MarkdownParser()
        bad = b"hello \xff world"
        out = await p.convert(bad, "x.md")
        assert "hello" in out and "world" in out

    def test_supports_md_extensions(self) -> None:
        p = MarkdownParser()
        assert p.supports("notes.md")
        assert p.supports("readme.MD")
        assert p.supports("notes.markdown")
        assert p.supports("notes.txt")

    def test_supports_by_content_type(self) -> None:
        p = MarkdownParser()
        assert p.supports("anything", "text/markdown")
        assert p.supports("anything", "text/plain")

    def test_supports_empty_filename(self) -> None:
        p = MarkdownParser()
        assert p.supports("")  # don't-know-claim-it

    def test_name(self) -> None:
        assert MarkdownParser().name() == "markdown"


class TestParserRegistry:
    def test_register_and_pick(self) -> None:
        reg = ParserRegistry()
        reg.register(MarkdownParser())
        assert len(reg) == 1
        p = reg.pick("notes.md")
        assert isinstance(p, MarkdownParser)

    def test_pick_picks_first_supporter(self) -> None:
        class FakePDFParser(Parser):
            def name(self) -> str:
                return "fake-pdf"

            def supports(self, filename, content_type=None) -> bool:
                return filename.endswith(".pdf")

            async def convert(self, file_data, filename) -> str:
                return "(pdf text)"

        reg = ParserRegistry()
        reg.register(FakePDFParser())  # registered first
        reg.register(MarkdownParser())  # fallback
        assert reg.pick("doc.pdf").name() == "fake-pdf"
        assert reg.pick("notes.md").name() == "markdown"

    def test_unsupported_raises(self) -> None:
        reg = ParserRegistry()

        # Register a parser that doesn't support .exe files
        class StrictParser(Parser):
            def name(self) -> str:
                return "strict"

            def supports(self, filename, content_type=None) -> bool:
                return filename.endswith(".md")

            async def convert(self, file_data, filename) -> str:
                return ""

        reg.register(StrictParser())
        with pytest.raises(UnsupportedFileTypeError):
            reg.pick("malware.exe")


# ─── md_builder ───────────────────────────────────────────────────────


class TestMdBuilder:
    def test_empty_input_returns_empty_tree(self) -> None:
        tree = build_markdown_tree("", "doc1")
        assert tree.document_id == "doc1"
        assert tree.roots == []
        assert tree.node_count() == 0

    def test_whitespace_only_returns_empty_tree(self) -> None:
        tree = build_markdown_tree("   \n  \n", "doc1")
        assert tree.roots == []

    def test_no_headers_returns_synthetic_root(self) -> None:
        tree = build_markdown_tree("just some plain text\nno headings", "doc1")
        assert tree.node_count() == 1
        root = tree.roots[0]
        assert root.depth == 1
        assert root.title == "(untitled document)"
        assert "plain text" in root.text

    def test_single_h1(self) -> None:
        md = "# Top\n\nsome body text"
        tree = build_markdown_tree(md, "doc1")
        assert tree.node_count() == 1
        assert tree.roots[0].title == "Top"
        assert tree.roots[0].depth == 1
        assert "some body text" in tree.roots[0].text

    def test_h1_with_h2_children(self) -> None:
        md = "# Top\n\nintro\n\n## Sub A\n\nsub a body\n\n## Sub B\n\nsub b body"
        tree = build_markdown_tree(md, "doc1")
        assert tree.node_count() == 3
        root = tree.roots[0]
        assert root.title == "Top"
        assert len(root.children) == 2
        assert root.children[0].title == "Sub A"
        assert root.children[1].title == "Sub B"
        # All children's parent_id matches the root
        for c in root.children:
            assert c.parent_id == root.id
            assert c.depth == 2

    def test_deeply_nested(self) -> None:
        md = """# A

## B

### C

#### D"""
        tree = build_markdown_tree(md, "doc1")
        assert tree.node_count() == 4
        a = tree.roots[0]
        assert a.children[0].title == "B"
        assert a.children[0].children[0].title == "C"
        assert a.children[0].children[0].children[0].title == "D"

    def test_multiple_h1_roots(self) -> None:
        md = "# First\n\nfoo\n\n# Second\n\nbar"
        tree = build_markdown_tree(md, "doc1")
        assert len(tree.roots) == 2
        assert tree.roots[0].title == "First"
        assert tree.roots[1].title == "Second"
        assert all(r.parent_id is None for r in tree.roots)

    def test_skipped_depth_is_handled(self) -> None:
        """h1 → h3 (skipping h2) still nests correctly."""
        md = "# A\n\n### C\n\nbody"
        tree = build_markdown_tree(md, "doc1")
        assert tree.node_count() == 2
        a = tree.roots[0]
        assert a.children[0].title == "C"
        assert a.children[0].depth == 3

    def test_code_block_headers_ignored(self) -> None:
        """Headings inside ```fenced``` regions don't create nodes."""
        md = """# Real Heading

```python
# fake heading inside code
## also fake
```

actual body
"""
        tree = build_markdown_tree(md, "doc1")
        assert tree.node_count() == 1
        assert tree.roots[0].title == "Real Heading"

    def test_node_text_spans_to_next_header(self) -> None:
        md = "# A\n\nA body\n\n## B\n\nB body\n\n## C\n\nC body"
        tree = build_markdown_tree(md, "doc1")
        a = tree.roots[0]
        b = a.children[0]
        c = a.children[1]
        assert "A body" in a.text
        assert "B body" not in a.text  # A's body stops at B
        assert "B body" in b.text
        assert "C body" not in b.text
        assert "C body" in c.text

    def test_line_numbers_populated(self) -> None:
        md = "# A\n\nfoo\n\n## B\n\nbar"
        tree = build_markdown_tree(md, "doc1")
        a = tree.roots[0]
        b = a.children[0]
        assert a.line_start == 1
        assert b.line_start == 5

    def test_locomo_style_session_headers(self) -> None:
        """Sanity: LME/LoCoMo-style ``## Session N`` shape parses."""
        md = """## Session 1 (2024-01-01)

**user**: hi
**assistant**: hello

## Session 2 (2024-01-02)

**user**: how are you
**assistant**: well thanks
"""
        tree = build_markdown_tree(md, "doc1")
        # No h1 → two h2s become roots
        assert len(tree.roots) == 2
        assert tree.roots[0].title.startswith("Session 1")
        assert tree.roots[1].title.startswith("Session 2")
        assert "hi" in tree.roots[0].text
        assert "well thanks" in tree.roots[1].text

    def test_summary_is_none_in_phase1(self) -> None:
        """Phase 1 builds structure only; summarizer is Phase 2."""
        md = "# Top\n\nbody"
        tree = build_markdown_tree(md, "doc1")
        for n in tree.all_nodes():
            assert n.summary is None

    def test_unique_ids_per_node(self) -> None:
        md = "# A\n## B\n## C\n### D"
        tree = build_markdown_tree(md, "doc1")
        ids = [n.id for n in tree.all_nodes()]
        assert len(set(ids)) == len(ids)  # all unique


# ─── ConvertResult ────────────────────────────────────────────────────


class TestConvertResult:
    def test_default_mime(self) -> None:
        r = ConvertResult(content="abc", parser_name="markdown")
        assert r.mime_type == "text/markdown"
