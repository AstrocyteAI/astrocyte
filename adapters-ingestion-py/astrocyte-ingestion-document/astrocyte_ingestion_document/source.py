"""Local document folder poll :class:`~astrocyte.ingest.source.IngestSource`.

Watches a local directory, ingesting new or modified files every
``interval_seconds`` seconds.  Change detection uses mtime + file size
so it works without inotify (cross-platform, works inside containers).

Configuration (``sources`` block in ``astrocyte.yaml``)
-------------------------------------------------------
    type: poll
    driver: document
    path: /data/documents/         # folder to watch (recursive)
    interval_seconds: 120
    target_bank: my-bank
    extraction_profile: document

Optional settings (via ``options`` dict in SourceConfig if present, or
fall through to sane defaults):
    recursive: true     # watch subdirectories (default: true)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path
from typing import Any

from astrocyte.config import SourceConfig
from astrocyte.errors import IngestError
from astrocyte.ingest.bank_resolve import resolve_ingest_bank_id
from astrocyte.ingest.logutil import log_ingest_event
from astrocyte.ingest.webhook import RetainCallable
from astrocyte.types import AstrocyteContext, HealthStatus

from astrocyte_ingestion_document._extract import extract_text

logger = logging.getLogger("astrocyte_ingestion_document")

# (mtime_ns, size) tuple used for change detection.
_FileState = tuple[int, int]


class DocumentFolderIngestSource:
    """Poll a local directory and retain text extracted from documents."""

    def __init__(
        self,
        source_id: str,
        config: SourceConfig,
        *,
        retain: RetainCallable,
    ) -> None:
        self._source_id = source_id
        self._config = config
        self._retain = retain
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._running = False
        self._last_error: str | None = None
        # path -> (mtime_ns, size) for change detection
        self._seen: dict[str, _FileState] = {}

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def source_type(self) -> str:
        return "poll"

    @property
    def config(self) -> SourceConfig:
        return self._config

    def _folder(self) -> Path:
        p = (self._config.path or "").strip()
        if not p:
            raise IngestError("document source requires path: /path/to/folder")
        folder = Path(p)
        if not folder.exists():
            raise IngestError(f"document source path does not exist: {folder}")
        if not folder.is_dir():
            raise IngestError(f"document source path must be a directory: {folder}")
        return folder

    def _interval_s(self) -> float:
        n = self._config.interval_seconds
        if n is None or int(n) < 10:
            raise IngestError("document poll requires interval_seconds >= 10")
        return float(int(n))

    def _recursive(self) -> bool:
        # options dict in SourceConfig if it ever exists; default True
        opts = getattr(self._config, "options", None) or {}
        return bool(opts.get("recursive", True))

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._last_error = None
        self._running = True
        # Validate early
        self._folder()
        self._interval_s()
        self._task = asyncio.create_task(
            self._run_loop(), name=f"astrocyte-document-poll-{self._source_id}"
        )

    async def stop(self) -> None:
        self._running = False
        self._stop.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def health_check(self) -> HealthStatus:
        if not self._running or self._task is None:
            return HealthStatus(healthy=False, message="document poll source stopped")
        if self._last_error:
            return HealthStatus(healthy=False, message=self._last_error)
        return HealthStatus(healthy=True, message="document poll loop running")

    async def _run_loop(self) -> None:
        interval = self._interval_s()
        while not self._stop.is_set():
            try:
                await self._poll_once()
                self._last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)
                log_ingest_event(
                    logger,
                    "document_poll_cycle_failed",
                    source_id=self._source_id,
                    error=str(exc),
                )
                logger.exception("document poll failed for %s", self._source_id)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                continue

    async def _poll_once(self) -> None:
        folder = self._folder()
        recursive = self._recursive()

        loop = asyncio.get_event_loop()
        # Enumerate files in executor to avoid blocking the event loop on large dirs.
        files: list[Path] = await loop.run_in_executor(None, self._list_files, folder, recursive)

        for fpath in files:
            try:
                stat = fpath.stat()
            except OSError:
                continue
            state: _FileState = (stat.st_mtime_ns, stat.st_size)
            key = str(fpath)
            if self._seen.get(key) == state:
                continue
            await self._ingest_file(fpath, state)

    @staticmethod
    def _list_files(folder: Path, recursive: bool) -> list[Path]:
        result: list[Path] = []
        if recursive:
            for root, _dirs, files in os.walk(folder):
                for fname in files:
                    result.append(Path(root) / fname)
        else:
            result = [f for f in folder.iterdir() if f.is_file()]
        return result

    async def _ingest_file(self, fpath: Path, state: _FileState) -> None:
        loop = asyncio.get_event_loop()
        # Extract text in executor (CPU-bound for PDF/DOCX)
        try:
            text: str | None = await loop.run_in_executor(None, extract_text, fpath)
        except Exception as exc:
            logger.warning("document ingest: extraction error for %s: %s", fpath, exc)
            return

        if not text or not text.strip():
            logger.debug("document ingest: no text extracted from %s", fpath)
            self._seen[str(fpath)] = state
            return

        try:
            bank_id = resolve_ingest_bank_id(self._config)
        except IngestError as exc:
            logger.warning("document poll %s skip file=%s: %s", self._source_id, fpath, exc)
            return

        p = self._config.principal
        ctx = AstrocyteContext(principal=p) if p else None
        metadata: dict[str, Any] = {
            "document": {
                "path": str(fpath),
                "filename": fpath.name,
                "extension": fpath.suffix.lower(),
                "size_bytes": state[1],
            }
        }

        await self._retain(
            text,
            bank_id,
            metadata=metadata,
            content_type="document",
            extraction_profile=self._config.extraction_profile,
            source=self._source_id,
            context=ctx,
        )
        self._seen[str(fpath)] = state
        log_ingest_event(
            logger,
            "document_file_ingested",
            source_id=self._source_id,
            path=str(fpath),
        )
