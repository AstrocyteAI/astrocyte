"""Tests for memory portability — AMA export and import."""

import json
from pathlib import Path

import pytest

from astrocytes._astrocyte import Astrocyte
from astrocytes.config import AstrocyteConfig
from astrocytes.portability import (
    AMA_VERSION,
    ImportResult,
    iter_ama_memories,
    read_ama_header,
)
from astrocytes.testing.in_memory import InMemoryEngineProvider


def _make_brain() -> tuple[Astrocyte, InMemoryEngineProvider]:
    config = AstrocyteConfig()
    config.provider = "test"
    config.barriers.pii.mode = "disabled"
    brain = Astrocyte(config)
    engine = InMemoryEngineProvider()
    brain.set_engine_provider(engine)
    return brain, engine


# ---------------------------------------------------------------------------
# AMA format — low-level reader/writer
# ---------------------------------------------------------------------------


class TestAmaHeader:
    async def test_export_creates_valid_header(self, tmp_path: Path):
        brain, engine = _make_brain()
        await brain.retain("test memory", bank_id="b1")
        output = tmp_path / "export.ama.jsonl"

        await brain.export_bank("b1", str(output))

        header = read_ama_header(output)
        assert header._ama_version == AMA_VERSION
        assert header.bank_id == "b1"
        assert header.memory_count >= 1
        assert header.exported_at  # ISO 8601 string

    def test_read_header_missing_version(self, tmp_path: Path):
        bad_file = tmp_path / "bad.jsonl"
        bad_file.write_text('{"bank_id": "b1"}\n')
        with pytest.raises(ValueError, match="missing _ama_version"):
            read_ama_header(bad_file)

    def test_read_header_empty_file(self, tmp_path: Path):
        empty = tmp_path / "empty.jsonl"
        empty.write_text("")
        with pytest.raises(ValueError, match="empty"):
            read_ama_header(empty)

    def test_read_header_wrong_version(self, tmp_path: Path):
        bad = tmp_path / "v99.jsonl"
        bad.write_text(
            '{"_ama_version": 99, "bank_id": "b1", "exported_at": "2026-01-01", "provider": "x", "memory_count": 0}\n'
        )
        with pytest.raises(ValueError, match="Unsupported AMA version"):
            read_ama_header(bad)


class TestAmaMemories:
    async def test_export_then_read(self, tmp_path: Path):
        brain, engine = _make_brain()
        await brain.retain("Calvin prefers dark mode", bank_id="b1")
        await brain.retain("Team uses GitHub Actions", bank_id="b1")
        output = tmp_path / "export.ama.jsonl"

        count = await brain.export_bank("b1", str(output))
        assert count >= 2

        memories = iter_ama_memories(output)
        assert len(memories) >= 2
        texts = [m.text for m in memories]
        assert any("dark mode" in t for t in texts)
        assert any("GitHub Actions" in t for t in texts)

    async def test_memory_fields(self, tmp_path: Path):
        brain, engine = _make_brain()
        await brain.retain("Tagged content", bank_id="b1", tags=["pref"], metadata={"key": "val"})
        output = tmp_path / "export.ama.jsonl"

        await brain.export_bank("b1", str(output))
        memories = iter_ama_memories(output)
        assert len(memories) >= 1

        mem = memories[0]
        assert mem.id  # Should have a memory ID
        assert mem.text == "Tagged content"
        assert mem.tags == ["pref"]

    async def test_ama_is_valid_jsonl(self, tmp_path: Path):
        brain, engine = _make_brain()
        await brain.retain("Line one", bank_id="b1")
        await brain.retain("Line two", bank_id="b1")
        output = tmp_path / "export.ama.jsonl"

        await brain.export_bank("b1", str(output))

        # Every line must be valid JSON
        with open(output) as f:
            for i, line in enumerate(f):
                data = json.loads(line)  # Should not raise
                assert isinstance(data, dict)


# ---------------------------------------------------------------------------
# Export/import round-trip
# ---------------------------------------------------------------------------


class TestExportImportRoundTrip:
    async def test_basic_round_trip(self, tmp_path: Path):
        # Export from brain A
        brain_a, engine_a = _make_brain()
        await brain_a.retain("Memory one about Python", bank_id="source")
        await brain_a.retain("Memory two about Rust", bank_id="source")
        export_path = tmp_path / "export.ama.jsonl"
        exported = await brain_a.export_bank("source", str(export_path))
        assert exported >= 2

        # Import into brain B (fresh)
        brain_b, engine_b = _make_brain()
        result = await brain_b.import_bank("target", str(export_path))
        assert isinstance(result, ImportResult)
        assert result.imported >= 2
        assert result.errors == 0

        # Verify imported memories are recallable
        recall_result = await brain_b.recall("Python", bank_id="target")
        assert len(recall_result.hits) >= 1

    async def test_import_preserves_tags(self, tmp_path: Path):
        brain_a, _ = _make_brain()
        await brain_a.retain("Tagged memory", bank_id="src", tags=["important"])
        export_path = tmp_path / "export.ama.jsonl"
        await brain_a.export_bank("src", str(export_path))

        brain_b, engine_b = _make_brain()
        await brain_b.import_bank("dst", str(export_path))

        # Check the imported memory has tags
        memories = engine_b._memories.get("dst", [])
        assert len(memories) >= 1
        assert memories[0].tags == ["important"]

    async def test_import_to_different_bank(self, tmp_path: Path):
        brain_a, _ = _make_brain()
        await brain_a.retain("Source content", bank_id="original")
        export_path = tmp_path / "export.ama.jsonl"
        await brain_a.export_bank("original", str(export_path))

        brain_b, engine_b = _make_brain()
        result = await brain_b.import_bank("new-bank", str(export_path))
        assert result.imported >= 1
        assert "new-bank" in engine_b._memories

    async def test_import_sets_source(self, tmp_path: Path):
        brain_a, _ = _make_brain()
        await brain_a.retain("Content", bank_id="b1")
        export_path = tmp_path / "export.ama.jsonl"
        await brain_a.export_bank("b1", str(export_path))

        brain_b, engine_b = _make_brain()
        await brain_b.import_bank("b2", str(export_path))

        memories = engine_b._memories.get("b2", [])
        assert len(memories) >= 1
        # Source should indicate AMA import
        assert memories[0].source is not None
        assert "import:ama" in memories[0].source


class TestImportConflictHandling:
    async def test_skip_duplicates(self, tmp_path: Path):
        brain, engine = _make_brain()
        await brain.retain("Existing content", bank_id="b1")
        export_path = tmp_path / "export.ama.jsonl"
        await brain.export_bank("b1", str(export_path))

        # Import same content again — should skip duplicates
        result = await brain.import_bank("b1", str(export_path), on_conflict="skip")
        # Exact behavior depends on dedup detection; at minimum no errors
        assert result.errors == 0


class TestImportProgress:
    async def test_progress_callback(self, tmp_path: Path):
        brain, _ = _make_brain()
        for i in range(15):
            await brain.retain(f"Memory number {i} about testing progress", bank_id="b1")
        export_path = tmp_path / "export.ama.jsonl"
        await brain.export_bank("b1", str(export_path))

        brain_b, _ = _make_brain()
        progress_calls: list[tuple[int, int]] = []

        def on_progress(imported: int, total: int) -> None:
            progress_calls.append((imported, total))

        await brain_b.import_bank("b2", str(export_path), progress_fn=on_progress)
        assert len(progress_calls) >= 1  # Called at least once


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


class TestPortabilityHooks:
    async def test_export_fires_hook(self, tmp_path: Path):
        brain, _ = _make_brain()
        await brain.retain("content", bank_id="b1")

        events = []
        brain.register_hook("on_export", lambda e: events.append(e))

        await brain.export_bank("b1", str(tmp_path / "out.jsonl"))
        assert len(events) == 1
        assert events[0].type == "on_export"
        assert events[0].data["memory_count"] >= 1

    async def test_import_fires_hook(self, tmp_path: Path):
        brain, _ = _make_brain()
        await brain.retain("content", bank_id="b1")
        export_path = tmp_path / "out.jsonl"
        await brain.export_bank("b1", str(export_path))

        events = []
        brain.register_hook("on_import", lambda e: events.append(e))

        await brain.import_bank("b2", str(export_path))
        assert len(events) == 1
        assert events[0].type == "on_import"
        assert events[0].data["imported"] >= 1
