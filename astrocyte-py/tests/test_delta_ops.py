"""Tests for delta operations on structured mental-model documents (M21 Phase A).

Locks in: each op type works correctly, invalid ops are dropped with a
reason, zero-op refresh produces an identical document (no drift),
adversarial LLM output never makes the doc worse than its input.
"""

from __future__ import annotations

import pytest

from astrocyte.pipeline.delta_ops import (
    AddSectionOp,
    AppendBlockOp,
    DeltaOperationList,
    InsertBlockOp,
    RemoveBlockOp,
    RemoveSectionOp,
    RenameSectionOp,
    ReplaceBlockOp,
    ReplaceSectionBlocksOp,
    apply_operations,
)
from astrocyte.pipeline.structured_doc import (
    BulletListBlock,
    ParagraphBlock,
    Section,
    StructuredDocument,
)


def _make_doc() -> StructuredDocument:
    return StructuredDocument(
        sections=[
            Section(
                id="intro",
                heading="Intro",
                level=2,
                blocks=[ParagraphBlock(text="Opening line.")],
            ),
            Section(
                id="bullets",
                heading="Bullets",
                level=2,
                blocks=[BulletListBlock(items=["a", "b"])],
            ),
        ],
    )


class TestZeroOps:
    def test_zero_ops_returns_identical_document(self):
        """The 'no change' contract: empty op list → identical doc dump."""
        doc = _make_doc()
        result = apply_operations(doc, [])
        assert result.document.model_dump() == doc.model_dump()
        assert result.applied == []
        assert result.skipped == []
        assert result.changed is False


class TestAppendBlock:
    def test_appends_to_named_section(self):
        doc = _make_doc()
        op = AppendBlockOp(section_id="intro", block=ParagraphBlock(text="Closing line."))
        r = apply_operations(doc, [op])
        section = r.document.section_by_id("intro")
        assert section is not None
        assert len(section.blocks) == 2
        assert isinstance(section.blocks[1], ParagraphBlock)
        assert section.blocks[1].text == "Closing line."
        assert r.changed is True
        assert len(r.applied) == 1
        assert r.skipped == []

    def test_unknown_section_id_skipped(self):
        doc = _make_doc()
        op = AppendBlockOp(section_id="missing", block=ParagraphBlock(text="x"))
        r = apply_operations(doc, [op])
        assert r.document.model_dump() == doc.model_dump()
        assert r.applied == []
        assert len(r.skipped) == 1
        assert "unknown section_id" in r.skipped[0]["reason"]


class TestInsertBlock:
    def test_inserts_at_index(self):
        doc = _make_doc()
        op = InsertBlockOp(
            section_id="intro",
            index=0,
            block=ParagraphBlock(text="Before."),
        )
        r = apply_operations(doc, [op])
        section = r.document.section_by_id("intro")
        assert section is not None
        assert len(section.blocks) == 2
        assert section.blocks[0].text == "Before."

    def test_insert_at_end_equals_append(self):
        doc = _make_doc()
        op = InsertBlockOp(
            section_id="intro",
            index=1,  # == len(blocks)
            block=ParagraphBlock(text="Tail."),
        )
        r = apply_operations(doc, [op])
        section = r.document.section_by_id("intro")
        assert section.blocks[1].text == "Tail."

    def test_out_of_range_skipped(self):
        doc = _make_doc()
        op = InsertBlockOp(
            section_id="intro",
            index=10,
            block=ParagraphBlock(text="x"),
        )
        r = apply_operations(doc, [op])
        assert r.applied == []
        assert "index out of range" in r.skipped[0]["reason"]


class TestReplaceBlock:
    def test_replaces_existing_block(self):
        doc = _make_doc()
        op = ReplaceBlockOp(
            section_id="intro",
            index=0,
            block=ParagraphBlock(text="Replaced."),
        )
        r = apply_operations(doc, [op])
        section = r.document.section_by_id("intro")
        assert section.blocks[0].text == "Replaced."

    def test_out_of_range_skipped(self):
        doc = _make_doc()
        op = ReplaceBlockOp(
            section_id="intro",
            index=5,
            block=ParagraphBlock(text="x"),
        )
        r = apply_operations(doc, [op])
        assert r.applied == []
        assert "out of range" in r.skipped[0]["reason"]


class TestRemoveBlock:
    def test_removes_block(self):
        doc = _make_doc()
        r = apply_operations(doc, [RemoveBlockOp(section_id="intro", index=0)])
        section = r.document.section_by_id("intro")
        assert section is not None
        assert section.blocks == []

    def test_out_of_range_skipped(self):
        doc = _make_doc()
        r = apply_operations(doc, [RemoveBlockOp(section_id="intro", index=99)])
        assert r.applied == []
        assert len(r.skipped) == 1


class TestAddSection:
    def test_appended_at_end_by_default(self):
        doc = _make_doc()
        op = AddSectionOp(
            heading="New",
            level=2,
            blocks=[ParagraphBlock(text="body")],
        )
        r = apply_operations(doc, [op])
        assert len(r.document.sections) == 3
        assert r.document.sections[-1].heading == "New"
        assert r.applied[0]["assigned_id"] == "new"

    def test_inserted_after_named(self):
        doc = _make_doc()
        op = AddSectionOp(heading="Middle", after_section_id="intro")
        r = apply_operations(doc, [op])
        assert [s.heading for s in r.document.sections] == ["Intro", "Middle", "Bullets"]

    def test_unknown_after_section_skipped(self):
        doc = _make_doc()
        op = AddSectionOp(heading="New", after_section_id="missing")
        r = apply_operations(doc, [op])
        assert r.document.model_dump() == doc.model_dump()
        assert "unknown after_section_id" in r.skipped[0]["reason"]

    def test_id_collision_auto_disambiguated(self):
        doc = _make_doc()
        op = AddSectionOp(heading="Intro")  # collides with existing "intro"
        r = apply_operations(doc, [op])
        assert r.applied[0]["assigned_id"] == "intro-2"


class TestRemoveSection:
    def test_removes_section(self):
        doc = _make_doc()
        r = apply_operations(doc, [RemoveSectionOp(section_id="intro")])
        assert len(r.document.sections) == 1
        assert r.document.sections[0].id == "bullets"

    def test_unknown_skipped(self):
        doc = _make_doc()
        r = apply_operations(doc, [RemoveSectionOp(section_id="missing")])
        assert r.applied == []
        assert len(r.skipped) == 1


class TestReplaceSectionBlocks:
    def test_replaces_all_blocks_preserves_heading_and_id(self):
        doc = _make_doc()
        op = ReplaceSectionBlocksOp(
            section_id="intro",
            blocks=[
                ParagraphBlock(text="A"),
                ParagraphBlock(text="B"),
            ],
        )
        r = apply_operations(doc, [op])
        section = r.document.section_by_id("intro")
        assert section is not None
        assert section.heading == "Intro"
        assert section.id == "intro"
        assert len(section.blocks) == 2


class TestRenameSection:
    def test_renames_heading_keeps_id(self):
        doc = _make_doc()
        op = RenameSectionOp(section_id="intro", new_heading="Introduction")
        r = apply_operations(doc, [op])
        section = r.document.section_by_id("intro")
        assert section.id == "intro"
        assert section.heading == "Introduction"


class TestAdversarialLLMOutput:
    """The contract: the structure can only get better or stay the same per refresh, never get worse."""

    def test_all_invalid_ops_leave_doc_unchanged(self):
        doc = _make_doc()
        bad_ops = [
            AppendBlockOp(section_id="missing", block=ParagraphBlock(text="x")),
            RemoveBlockOp(section_id="missing", index=0),
            RemoveSectionOp(section_id="missing"),
            ReplaceBlockOp(section_id="intro", index=99, block=ParagraphBlock(text="x")),
            InsertBlockOp(section_id="intro", index=999, block=ParagraphBlock(text="x")),
        ]
        r = apply_operations(doc, bad_ops)
        assert r.document.model_dump() == doc.model_dump()
        assert r.applied == []
        assert len(r.skipped) == len(bad_ops)

    def test_valid_ops_proceed_even_when_others_fail(self):
        """Per-op validation: one bad op doesn't poison the batch."""
        doc = _make_doc()
        ops = [
            AppendBlockOp(section_id="missing", block=ParagraphBlock(text="x")),
            AppendBlockOp(section_id="intro", block=ParagraphBlock(text="Added.")),
            RemoveSectionOp(section_id="ghost"),
        ]
        r = apply_operations(doc, ops)
        assert len(r.applied) == 1
        assert len(r.skipped) == 2
        section = r.document.section_by_id("intro")
        assert section.blocks[-1].text == "Added."


class TestDeltaOperationListSchema:
    """The container that LLMs emit JSON for — used in mental_model_compile."""

    def test_parses_json_op_list(self):
        payload = {
            "operations": [
                {
                    "op": "append_block",
                    "section_id": "intro",
                    "block": {"type": "paragraph", "text": "Added."},
                },
                {
                    "op": "add_section",
                    "heading": "New",
                    "blocks": [],
                },
            ]
        }
        parsed = DeltaOperationList.model_validate(payload)
        assert len(parsed.operations) == 2

    def test_unknown_op_rejected_at_schema_level(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            DeltaOperationList.model_validate(
                {"operations": [{"op": "warp_drive", "section_id": "x"}]},
            )
