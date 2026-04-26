"""Lifecycle and behaviour tests for DocumentFolderIngestSource."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from astrocyte.config import SourceConfig
from astrocyte.types import RetainResult

from astrocyte_ingestion_document import DocumentFolderIngestSource

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(folder: Path, **overrides: Any) -> SourceConfig:
    defaults: dict[str, Any] = {
        "type": "poll",
        "driver": "document",
        "path": str(folder),
        "interval_seconds": 30,
        "target_bank": "bank-a",
    }
    defaults.update(overrides)
    return SourceConfig(**defaults)


async def _noop_retain(text: str, bank_id: str, **kwargs: Any) -> RetainResult:
    return RetainResult(stored=True, memory_id="m1")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_stop(tmp_path: Path) -> None:
    src = DocumentFolderIngestSource("doc", _cfg(tmp_path), retain=_noop_retain)
    await src.start()
    assert src._running is True
    assert src._task is not None

    health = await src.health_check()
    assert health.healthy is True

    await src.stop()
    assert src._running is False
    assert src._task is None


@pytest.mark.asyncio
async def test_start_idempotent(tmp_path: Path) -> None:
    src = DocumentFolderIngestSource("doc", _cfg(tmp_path), retain=_noop_retain)
    await src.start()
    task_first = src._task
    await src.start()
    assert src._task is task_first
    await src.stop()


@pytest.mark.asyncio
async def test_health_stopped(tmp_path: Path) -> None:
    src = DocumentFolderIngestSource("doc", _cfg(tmp_path), retain=_noop_retain)
    health = await src.health_check()
    assert health.healthy is False


@pytest.mark.asyncio
async def test_nonexistent_path_raises(tmp_path: Path) -> None:
    from astrocyte.errors import IngestError

    cfg = _cfg(tmp_path / "does_not_exist")
    src = DocumentFolderIngestSource("doc", cfg, retain=_noop_retain)
    with pytest.raises(IngestError, match="does not exist"):
        await src.start()


@pytest.mark.asyncio
async def test_file_path_raises(tmp_path: Path) -> None:
    from astrocyte.errors import IngestError

    f = tmp_path / "file.txt"
    f.write_text("hello")
    src = DocumentFolderIngestSource("doc", _cfg(f), retain=_noop_retain)
    with pytest.raises(IngestError, match="directory"):
        await src.start()


# ---------------------------------------------------------------------------
# _poll_once — file ingestion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_once_ingests_text_file(tmp_path: Path) -> None:
    retained: list[tuple[str, str]] = []

    async def retain(text: str, bank_id: str, **kwargs: Any) -> RetainResult:
        retained.append((text, bank_id))
        return RetainResult(stored=True, memory_id="m1")

    (tmp_path / "hello.txt").write_text("Hello from folder!", encoding="utf-8")

    src = DocumentFolderIngestSource("doc", _cfg(tmp_path), retain=retain)
    await src._poll_once()

    assert len(retained) == 1
    assert retained[0][1] == "bank-a"
    assert "Hello from folder!" in retained[0][0]


@pytest.mark.asyncio
async def test_poll_once_skips_unchanged_file(tmp_path: Path) -> None:
    retained: list[str] = []

    async def retain(text: str, bank_id: str, **kwargs: Any) -> RetainResult:
        retained.append(text)
        return RetainResult(stored=True, memory_id="m1")

    f = tmp_path / "notes.txt"
    f.write_text("Stable content", encoding="utf-8")

    src = DocumentFolderIngestSource("doc", _cfg(tmp_path), retain=retain)
    await src._poll_once()  # first pass — ingest
    assert len(retained) == 1

    await src._poll_once()  # second pass — unchanged, skip
    assert len(retained) == 1


@pytest.mark.asyncio
async def test_poll_once_reingest_on_change(tmp_path: Path) -> None:
    retained: list[str] = []

    async def retain(text: str, bank_id: str, **kwargs: Any) -> RetainResult:
        retained.append(text)
        return RetainResult(stored=True, memory_id="m1")

    f = tmp_path / "notes.txt"
    f.write_text("Version 1", encoding="utf-8")

    src = DocumentFolderIngestSource("doc", _cfg(tmp_path), retain=retain)
    await src._poll_once()
    assert len(retained) == 1

    # Overwrite — mtime and size change
    f.write_text("Version 2 longer content here", encoding="utf-8")
    await src._poll_once()
    assert len(retained) == 2
    assert "Version 2" in retained[1]


@pytest.mark.asyncio
async def test_poll_once_recursive(tmp_path: Path) -> None:
    retained: list[str] = []

    async def retain(text: str, bank_id: str, **kwargs: Any) -> RetainResult:
        retained.append(text)
        return RetainResult(stored=True, memory_id="m1")

    sub = tmp_path / "subdir"
    sub.mkdir()
    (tmp_path / "root.txt").write_text("Root file", encoding="utf-8")
    (sub / "nested.txt").write_text("Nested file", encoding="utf-8")

    src = DocumentFolderIngestSource("doc", _cfg(tmp_path), retain=retain)
    await src._poll_once()

    texts = [r for r in retained]
    assert any("Root file" in t for t in texts)
    assert any("Nested file" in t for t in texts)



@pytest.mark.asyncio
async def test_poll_once_multiple_formats(tmp_path: Path) -> None:
    pytest.importorskip("docx")
    import io as _io

    from docx import Document

    retained: list[str] = []

    async def retain(text: str, bank_id: str, **kwargs: Any) -> RetainResult:
        retained.append(text)
        return RetainResult(stored=True, memory_id="m1")

    (tmp_path / "note.txt").write_text("Plain text", encoding="utf-8")
    (tmp_path / "page.html").write_bytes(b"<p>HTML content</p>")

    doc = Document()
    doc.add_paragraph("DOCX paragraph")
    buf = _io.BytesIO()
    doc.save(buf)
    (tmp_path / "report.docx").write_bytes(buf.getvalue())

    src = DocumentFolderIngestSource("doc", _cfg(tmp_path), retain=retain)
    await src._poll_once()

    all_text = " ".join(retained)
    assert "Plain text" in all_text
    assert "HTML content" in all_text
    assert "DOCX paragraph" in all_text


@pytest.mark.asyncio
async def test_poll_metadata_includes_path(tmp_path: Path) -> None:
    meta_captured: list[dict] = []

    async def retain(text: str, bank_id: str, **kwargs: Any) -> RetainResult:
        m = kwargs.get("metadata") or {}
        meta_captured.append(m)
        return RetainResult(stored=True, memory_id="m1")

    (tmp_path / "doc.txt").write_text("Content", encoding="utf-8")

    src = DocumentFolderIngestSource("doc", _cfg(tmp_path), retain=retain)
    await src._poll_once()

    assert len(meta_captured) == 1
    doc_meta = meta_captured[0].get("document", {})
    assert "doc.txt" in doc_meta.get("filename", "")
    assert doc_meta.get("extension") == ".txt"
    assert "size_bytes" in doc_meta
