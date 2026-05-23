"""Tests for the structured-doc schema, renderer, and parser (M21 Phase A).

Locks in: render fidelity, round-trip safety (parse → render → parse
identical), slug stability, implicit-section fallback for pre-heading
content.
"""

from __future__ import annotations

from astrocyte.pipeline.structured_doc import (
    BulletListBlock,
    CodeBlock,
    OrderedListBlock,
    ParagraphBlock,
    Section,
    StructuredDocument,
    make_unique_id,
    parse_markdown,
    render_block,
    render_document,
    render_section,
    slugify_heading,
)


class TestSlugify:
    def test_simple_heading(self):
        assert slugify_heading("Stop Conditions") == "stop-conditions"

    def test_punctuation_and_case(self):
        assert slugify_heading("Inputs & Context!") == "inputs-context"

    def test_only_punctuation_fallback(self):
        assert slugify_heading("---") == "section"

    def test_empty_fallback(self):
        assert slugify_heading("") == "section"


class TestMakeUniqueId:
    def test_no_collision_returns_base(self):
        assert make_unique_id("foo", set()) == "foo"

    def test_collision_appends_number(self):
        assert make_unique_id("foo", {"foo"}) == "foo-2"

    def test_chained_collisions(self):
        assert make_unique_id("foo", {"foo", "foo-2", "foo-3"}) == "foo-4"


class TestRenderBlock:
    def test_paragraph(self):
        assert render_block(ParagraphBlock(text="Hello world.")) == "Hello world."

    def test_paragraph_strips_trailing_ws(self):
        assert render_block(ParagraphBlock(text="Hi.   ")) == "Hi."

    def test_bullet_list(self):
        b = BulletListBlock(items=["one", "two", "three"])
        assert render_block(b) == "- one\n- two\n- three"

    def test_ordered_list(self):
        b = OrderedListBlock(items=["first", "second"])
        assert render_block(b) == "1. first\n2. second"

    def test_code_with_language(self):
        b = CodeBlock(language="python", text="x = 1")
        assert render_block(b) == "```python\nx = 1\n```"

    def test_code_without_language(self):
        b = CodeBlock(text="plain")
        assert render_block(b) == "```\nplain\n```"


class TestRenderSection:
    def test_heading_only(self):
        s = Section(id="x", heading="Hello", level=2)
        assert render_section(s) == "## Hello"

    def test_heading_with_paragraph(self):
        s = Section(
            id="x",
            heading="Hello",
            level=2,
            blocks=[ParagraphBlock(text="Body text.")],
        )
        assert render_section(s) == "## Hello\n\nBody text."

    def test_heading_with_multiple_blocks(self):
        s = Section(
            id="x",
            heading="X",
            level=3,
            blocks=[
                ParagraphBlock(text="P1"),
                BulletListBlock(items=["a", "b"]),
            ],
        )
        assert render_section(s) == "### X\n\nP1\n\n- a\n- b"


class TestRenderDocument:
    def test_empty(self):
        assert render_document(StructuredDocument()) == ""

    def test_single_section_has_trailing_newline(self):
        doc = StructuredDocument(
            sections=[Section(id="x", heading="Hello", level=1)],
        )
        assert render_document(doc) == "# Hello\n"

    def test_multiple_sections_separated_by_blank_line(self):
        doc = StructuredDocument(
            sections=[
                Section(id="a", heading="A", level=2),
                Section(id="b", heading="B", level=2),
            ],
        )
        assert render_document(doc) == "## A\n\n## B\n"


class TestParseMarkdown:
    def test_empty_input(self):
        doc = parse_markdown("")
        assert doc.sections == []

    def test_single_heading(self):
        doc = parse_markdown("## Hello")
        assert len(doc.sections) == 1
        s = doc.sections[0]
        assert s.heading == "Hello"
        assert s.id == "hello"
        assert s.level == 2

    def test_heading_with_paragraph(self):
        doc = parse_markdown("## Hello\n\nBody text here.")
        assert len(doc.sections) == 1
        assert len(doc.sections[0].blocks) == 1
        assert isinstance(doc.sections[0].blocks[0], ParagraphBlock)
        assert doc.sections[0].blocks[0].text == "Body text here."

    def test_pre_heading_content_wraps_in_overview(self):
        doc = parse_markdown("Some intro text.\n\n## Real Section")
        assert len(doc.sections) == 2
        assert doc.sections[0].heading == "Overview"
        assert doc.sections[0].id == "overview"
        assert doc.sections[1].heading == "Real Section"

    def test_duplicate_headings_get_unique_slugs(self):
        doc = parse_markdown("## Foo\n\n## Foo")
        assert doc.sections[0].id == "foo"
        assert doc.sections[1].id == "foo-2"

    def test_bullet_list(self):
        doc = parse_markdown("## H\n\n- one\n- two")
        assert isinstance(doc.sections[0].blocks[0], BulletListBlock)
        assert doc.sections[0].blocks[0].items == ["one", "two"]

    def test_ordered_list(self):
        doc = parse_markdown("## H\n\n1. one\n2. two")
        assert isinstance(doc.sections[0].blocks[0], OrderedListBlock)
        assert doc.sections[0].blocks[0].items == ["one", "two"]

    def test_code_block_with_language(self):
        doc = parse_markdown("## H\n\n```python\nx = 1\n```")
        b = doc.sections[0].blocks[0]
        assert isinstance(b, CodeBlock)
        assert b.language == "python"
        assert b.text == "x = 1"

    def test_horizontal_rule_treated_as_separator(self):
        doc = parse_markdown("## A\n\nBody.\n\n---\n\n## B\n\nMore.")
        assert len(doc.sections) == 2
        assert doc.sections[0].heading == "A"
        assert doc.sections[1].heading == "B"


class TestRoundTrip:
    def test_simple_doc_round_trip_byte_identical(self):
        doc = StructuredDocument(
            sections=[
                Section(
                    id="intro",
                    heading="Intro",
                    level=2,
                    blocks=[ParagraphBlock(text="Hello.")],
                ),
                Section(
                    id="bullets",
                    heading="Bullets",
                    level=2,
                    blocks=[BulletListBlock(items=["a", "b", "c"])],
                ),
            ],
        )
        md = render_document(doc)
        reparsed = parse_markdown(md)
        assert render_document(reparsed) == md

    def test_code_block_round_trip(self):
        doc = StructuredDocument(
            sections=[
                Section(
                    id="x",
                    heading="X",
                    level=2,
                    blocks=[CodeBlock(language="python", text="print('hi')")],
                ),
            ],
        )
        md = render_document(doc)
        reparsed = parse_markdown(md)
        assert render_document(reparsed) == md
