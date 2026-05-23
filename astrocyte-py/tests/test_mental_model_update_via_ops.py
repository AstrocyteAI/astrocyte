"""Tests for MentalModelService.update_via_ops + InMemoryMentalModelStore (M21 Phase C).

Locks in: structured-doc lazy migration from legacy markdown rows,
revision bump on changed ops, no-op when ops don't apply, invalid op
list returns audit trail without bumping revision, content stays
synced with structured_doc.
"""

from __future__ import annotations

import pytest

from astrocyte.pipeline.mental_model import MentalModelService
from astrocyte.testing.in_memory import InMemoryMentalModelStore


@pytest.fixture
def service() -> MentalModelService:
    return MentalModelService(InMemoryMentalModelStore())


async def _seed(service: MentalModelService) -> str:
    """Create a legacy-shape mental model (structured_doc=None, raw markdown content)."""
    await service.create(
        bank_id="bank-1",
        model_id="m1",
        title="Alice prefs",
        content="## Intro\n\nAlice likes async.\n\n## Tools\n\n- Slack\n- Linear",
        scope="bank",
    )
    return "m1"


class TestUpdateViaOps:
    async def test_lazy_migrate_legacy_row_on_first_refresh(self, service):
        """The first op against a legacy row parses content → structured_doc and persists it."""
        await _seed(service)
        ops = [
            {
                "op": "append_block",
                "section_id": "tools",
                "block": {"type": "paragraph", "text": "(updated)"},
            },
        ]
        result = await service.update_via_ops(
            bank_id="bank-1", model_id="m1", operations=ops,
        )
        assert result is not None
        model, summary = result
        assert summary["changed"] is True
        assert len(summary["applied"]) == 1
        # structured_doc is now populated
        assert model.structured_doc is not None
        assert len(model.structured_doc["sections"]) == 2
        # content re-rendered from structured_doc; reflects the new block
        assert "(updated)" in model.content
        # revision bumped from 1 → 2
        assert model.revision == 2

    async def test_returns_none_for_missing_model(self, service):
        result = await service.update_via_ops(
            bank_id="bank-1", model_id="ghost", operations=[],
        )
        assert result is None

    async def test_zero_ops_does_not_bump_revision(self, service):
        await _seed(service)
        before = await service.get(bank_id="bank-1", model_id="m1")
        assert before is not None and before.revision == 1
        result = await service.update_via_ops(
            bank_id="bank-1", model_id="m1", operations=[],
        )
        assert result is not None
        _, summary = result
        assert summary["changed"] is False
        after = await service.get(bank_id="bank-1", model_id="m1")
        assert after is not None and after.revision == 1

    async def test_invalid_op_schema_drops_silently(self, service):
        await _seed(service)
        # ``warp_drive`` is not a real op — schema validation drops the batch.
        result = await service.update_via_ops(
            bank_id="bank-1",
            model_id="m1",
            operations=[{"op": "warp_drive", "section_id": "tools"}],
        )
        assert result is not None
        _, summary = result
        # Schema-invalid: no ops applied, doc unchanged, no revision bump
        assert summary["changed"] is False
        after = await service.get(bank_id="bank-1", model_id="m1")
        assert after is not None and after.revision == 1

    async def test_unknown_section_ops_dropped_per_op(self, service):
        await _seed(service)
        ops = [
            {
                "op": "append_block",
                "section_id": "ghost",  # unknown
                "block": {"type": "paragraph", "text": "x"},
            },
            {
                "op": "append_block",
                "section_id": "tools",  # valid
                "block": {"type": "paragraph", "text": "kept"},
            },
        ]
        result = await service.update_via_ops(
            bank_id="bank-1", model_id="m1", operations=ops,
        )
        assert result is not None
        model, summary = result
        # 1 applied, 1 skipped; revision bumped because at least one applied.
        assert summary["changed"] is True
        assert len(summary["applied"]) == 1
        assert len(summary["skipped"]) == 1
        assert "unknown section_id" in summary["skipped"][0]["reason"]
        assert model.revision == 2
        assert "kept" in model.content

    async def test_content_and_structured_doc_stay_in_sync(self, service):
        await _seed(service)
        ops = [
            {
                "op": "rename_section",
                "section_id": "intro",
                "new_heading": "Introduction",
            },
        ]
        result = await service.update_via_ops(
            bank_id="bank-1", model_id="m1", operations=ops,
        )
        assert result is not None
        model, _ = result
        # New heading appears in rendered markdown
        assert "## Introduction" in model.content
        assert "## Intro\n" not in model.content
        # Section id stays "intro" (per rename contract)
        assert model.structured_doc is not None
        section = next(s for s in model.structured_doc["sections"] if s["id"] == "intro")
        assert section["heading"] == "Introduction"

    async def test_add_section_op_creates_new_section(self, service):
        await _seed(service)
        ops = [
            {
                "op": "add_section",
                "heading": "Newsfeed",
                "blocks": [{"type": "paragraph", "text": "Today's note."}],
            },
        ]
        result = await service.update_via_ops(
            bank_id="bank-1", model_id="m1", operations=ops,
        )
        assert result is not None
        model, summary = result
        assert summary["applied"][0]["assigned_id"] == "newsfeed"
        assert "## Newsfeed" in model.content
        assert "Today's note." in model.content


class TestRefreshFromSources:
    """M28 — MentalModelService.refresh_from_sources covers Hindsight parity
    for ``mental_model.refresh()``. The in-memory store merges new sources
    (deduped) and bumps the revision; production rewires this to the LLM
    compile pipeline without breaking the SPI surface."""

    async def test_refresh_bumps_revision_and_merges_sources(self, service):
        # Seed with two initial sources.
        await service.create(
            bank_id="bank-1",
            model_id="m1",
            title="Alice prefs",
            content="## Intro\n\nAlice likes async.",
            scope="bank",
            source_ids=["mem-a", "mem-b"],
        )
        result = await service.refresh_from_sources(
            bank_id="bank-1",
            model_id="m1",
            new_source_ids=["mem-c", "mem-d"],
        )
        assert result is not None
        # Revision bumped 1 → 2 via upsert.
        assert result.revision == 2
        # Existing order preserved + novel sources appended.
        assert result.source_ids == ["mem-a", "mem-b", "mem-c", "mem-d"]

    async def test_refresh_missing_model_returns_none(self, service):
        result = await service.refresh_from_sources(
            bank_id="bank-1",
            model_id="ghost",
            new_source_ids=["mem-x"],
        )
        assert result is None

    async def test_refresh_dedups_duplicate_source_ids(self, service):
        # Seed with sources that overlap the refresh input.
        await service.create(
            bank_id="bank-1",
            model_id="m1",
            title="Alice prefs",
            content="## Intro\n\nAlice likes async.",
            scope="bank",
            source_ids=["mem-a", "mem-b"],
        )
        result = await service.refresh_from_sources(
            bank_id="bank-1",
            model_id="m1",
            # mem-a is already in source_ids; mem-c is new; mem-c repeated
            new_source_ids=["mem-a", "mem-c", "mem-c"],
        )
        assert result is not None
        assert result.revision == 2
        # Dedup: mem-a preserved once, mem-c added once.
        assert result.source_ids == ["mem-a", "mem-b", "mem-c"]
