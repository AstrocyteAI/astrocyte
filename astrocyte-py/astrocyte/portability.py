"""Memory portability — AMA (Astrocyte Memory Archive) export and import.

AMA is a newline-delimited JSON (JSONL) format. Line 1 is the header,
subsequent lines are individual memories. Streamable, self-describing,
and FFI-safe (plain JSON, no Python-specific types).

See docs/_design/memory-portability.md for the full specification.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Literal

from astrocyte.types import Metadata, RecallRequest, RecallResult, RetainRequest, VectorItem

logger = logging.getLogger("astrocyte.portability")

# ---------------------------------------------------------------------------
# Path containment (CWE-022)
# ---------------------------------------------------------------------------
#
# Path(path).resolve() canonicalises but does NOT contain — a caller can
# pass /etc/passwd and resolve() returns it unchanged.  ``_safe_resolve``
# validates that the resolved path stays within an explicit allow-list.
#
# Allow-list resolution order:
#   1. ``allowed_roots`` kwarg passed to the public function
#   2. ``ASTROCYTE_PORTABILITY_ROOTS`` env var (os.pathsep-joined)
#
# When neither (1) nor (2) is configured, ``_safe_resolve`` REFUSES to
# return a path unless the caller has explicitly opted into uncontained
# mode via ``allow_uncontained=True``.  This eliminates the silent
# "no containment" gap that CodeQL CWE-022 (py/path-injection) flags
# and forces every caller to make a conscious security decision.
#
# Recommended usage:
#   * Server / gateway code: set ``ASTROCYTE_PORTABILITY_ROOTS`` and
#     leave ``allow_uncontained=False``.  Untrusted HTTP input cannot
#     escape the configured roots.
#   * Library / CLI / unit tests with caller-controlled paths: pass
#     ``allowed_roots=[<known dir>]`` explicitly.
#   * Trusted internal call sites that genuinely need any path: pass
#     ``allow_uncontained=True`` to make the decision audit-able.

_PORTABILITY_ROOTS_ENV = "ASTROCYTE_PORTABILITY_ROOTS"

# Null byte and ASCII control characters never have a legitimate place in
# a filesystem path. Reject them up front; resolve() does NOT strip them.
_ILLEGAL_PATH_CHAR_ORDS = frozenset(range(0x00, 0x20)) | {0x7F}


def _portability_roots() -> list[Path]:
    """Read containment roots from the environment."""
    raw = os.environ.get(_PORTABILITY_ROOTS_ENV, "")
    return [Path(p).expanduser().resolve() for p in raw.split(os.pathsep) if p]


def _resolve_contained(
    path: str | Path,
    *,
    allowed_roots: list[str | Path] | None = None,
    allow_uncontained: bool = False,
) -> Path:
    """Resolve ``path`` and verify it stays within an allowed root.

    See module docstring for the allow-list resolution order and the
    ``allow_uncontained`` opt-in semantics.

    Raises:
        ValueError: If the path contains illegal control characters,
            escapes every allowed root, or no containment is configured
            and the caller did not pass ``allow_uncontained=True``.
    """
    path_str = os.fspath(path)
    if any(ord(c) in _ILLEGAL_PATH_CHAR_ORDS for c in path_str):
        raise ValueError(f"Portability path contains illegal control character: {path_str!r}")
    # ``path_str`` is user-controlled, but the taint is neutralized by
    # the allow-list containment check below — ``resolved`` is matched
    # against ``allowed_roots`` (or the env-configured
    # ``_portability_roots()``) before the function returns. Callers
    # cannot opt out without passing ``allow_uncontained=True``
    # explicitly. CodeQL's taint tracker doesn't see through allow-list
    # logic, so this whole file is in CodeQL's ``paths-ignore`` (see
    # ``.github/codeql/codeql-config.yml``). Threat model is locked by
    # ``tests/test_portability.py::TestPathContainment``.
    resolved = Path(path_str).expanduser().resolve()
    roots: list[Path]
    if allowed_roots:
        roots = [Path(r).expanduser().resolve() for r in allowed_roots]
    else:
        roots = _portability_roots()
    if not roots:
        if not allow_uncontained:
            raise ValueError(
                "Portability path containment is required. Provide one of:\n"
                "  - allowed_roots=[<dir>, ...] kwarg, OR\n"
                f"  - {_PORTABILITY_ROOTS_ENV} environment variable "
                "(os.pathsep-joined directories), OR\n"
                "  - allow_uncontained=True for trusted internal callers."
            )
        return resolved
    for root in roots:
        if resolved == root or resolved.is_relative_to(root):
            return resolved
    raise ValueError(f"Portability path escapes allowed roots: {resolved!s} not in {[str(r) for r in roots]}")


def _safe_resolve(
    path: str | Path,
    *,
    allowed_roots: list[str | Path] | None = None,
    allow_uncontained: bool = False,
) -> Path:
    """:func:`_resolve_contained`, with every rejection as a ``ValueError``.

    ``Path.expanduser()`` raises ``RuntimeError`` for a home directory it
    cannot find (``~nosuchuser``), and ``resolve()`` can for a symlink loop.
    Callers — the gateway maps ``ValueError`` to 422 — must see a rejected
    path, not an unhandled error.
    """
    try:
        return _resolve_contained(path, allowed_roots=allowed_roots, allow_uncontained=allow_uncontained)
    except RuntimeError as exc:
        raise ValueError(f"Portability path cannot be resolved: {os.fspath(path)!r} ({exc})") from exc


# ---------------------------------------------------------------------------
# AMA header
# ---------------------------------------------------------------------------

AMA_VERSION = 1


@dataclass
class AmaHeader:
    """First line of an AMA file."""

    bank_id: str
    exported_at: str  # ISO 8601
    provider: str
    memory_count: int
    _ama_version: int = AMA_VERSION


@dataclass
class AmaMemory:
    """One memory line in an AMA file."""

    id: str
    text: str
    fact_type: str | None = None
    tags: list[str] | None = None
    metadata: Metadata | None = None
    occurred_at: str | None = None  # ISO 8601
    created_at: str | None = None  # ISO 8601
    source: str | None = None
    bank_id: str | None = None
    entities: list[dict[str, str | list[str]]] | None = None
    embedding: list[float] | None = None


# ---------------------------------------------------------------------------
# Writer — export a bank to AMA JSONL
# ---------------------------------------------------------------------------


async def export_bank(
    recall_fn: Callable[[RecallRequest], Awaitable[RecallResult]] | None,
    bank_id: str,
    path: str | Path,
    provider_name: str = "unknown",
    include_embeddings: bool = False,
    include_entities: bool = True,
    batch_size: int = 100,
    *,
    list_fn: Callable[[str, int, int], Awaitable[list[VectorItem]]] | None = None,
    allowed_roots: list[str | Path] | None = None,
    allow_uncontained: bool = False,
) -> int:
    """Export a memory bank to AMA JSONL format.

    Memories are enumerated in one of two ways:

    * ``list_fn`` (preferred) — a vector store's ``list_vectors(bank_id,
      offset, limit)``. Paged by offset over the store's stable order, so
      every live memory in the bank is exported.
    * ``recall_fn`` (fallback) — a single relevance-ranked
      ``query="*"`` recall capped at ``batch_size`` hits. ``RecallRequest``
      has no offset, so this cannot page: a bank larger than
      ``batch_size`` is exported **incompletely**. It exists only for
      engine providers that expose no listing API; a warning is logged
      when the result may be truncated.

    Args:
        recall_fn: Async callable that takes a RecallRequest and returns
            RecallResult. Used only when ``list_fn`` is ``None``.
        bank_id: Bank to export.
        path: Output file path.
        provider_name: Provider identifier for the header.
        include_embeddings: Include vector embeddings (not portable across models).
        include_entities: Include extracted entities.
        batch_size: Page size for ``list_fn``; hit cap for ``recall_fn``.
        list_fn: Async ``(bank_id, offset, limit) -> list[VectorItem]``,
            typically ``vector_store.list_vectors``. Must return a stable
            order (see ``VectorStore.list_vectors``).
        allowed_roots: Optional list of directory roots; the resolved
            ``path`` must fall under one of them.  When ``None``, falls
            back to ``ASTROCYTE_PORTABILITY_ROOTS`` env var.
        allow_uncontained: When True, skip path containment if neither
            ``allowed_roots`` nor the env var is set.  Use only for
            trusted internal callers — the default ``False`` raises if
            no containment is configured.  See ``_safe_resolve``.

    Returns:
        Number of memories exported.
    """
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size}")
    if list_fn is None and recall_fn is None:
        raise ValueError("export_bank requires list_fn or recall_fn")
    path = _safe_resolve(path, allowed_roots=allowed_roots, allow_uncontained=allow_uncontained)
    path.parent.mkdir(parents=True, exist_ok=True)

    if list_fn is not None:
        records = await _records_from_listing(list_fn, bank_id, batch_size)
    else:
        records = await _records_from_recall(recall_fn, bank_id, batch_size)

    # Write AMA file
    now = datetime.now(timezone.utc).isoformat()
    header = {
        "_ama_version": AMA_VERSION,
        "bank_id": bank_id,
        "exported_at": now,
        "provider": provider_name,
        "memory_count": len(records),
    }

    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(header, default=str) + "\n")
        for record in records:
            f.write(json.dumps(record, default=str) + "\n")

    return len(records)


async def _records_from_listing(
    list_fn: Callable[[str, int, int], Awaitable[list[VectorItem]]],
    bank_id: str,
    batch_size: int,
) -> list[dict]:
    """Page through ``list_fn`` until it is exhausted."""
    records: list[dict] = []
    seen: set[str] = set()
    offset = 0
    while True:
        page = await list_fn(bank_id, offset, batch_size)
        if not page:
            break
        new = [item for item in page if item.id not in seen]
        if not new:
            # A store that ignores ``offset`` would otherwise loop forever.
            raise RuntimeError(
                f"export_bank: list_vectors returned no new memories at offset {offset} "
                f"for bank {bank_id!r}; the store's pagination is not stable"
            )
        for item in new:
            seen.add(item.id)
            records.append(
                _ama_record(
                    memory_id=item.id,
                    text=item.text,
                    fact_type=item.fact_type,
                    tags=item.tags,
                    metadata=item.metadata,
                    occurred_at=item.occurred_at,
                    source=None,
                    bank_id=item.bank_id,
                )
            )
        offset += len(page)
        if len(page) < batch_size:
            break
    return records


async def _records_from_recall(
    recall_fn: Callable[[RecallRequest], Awaitable[RecallResult]],
    bank_id: str,
    batch_size: int,
) -> list[dict]:
    """Best-effort enumeration for providers with no listing API.

    One ``query="*"`` recall, capped at ``batch_size``. Results are
    relevance-ranked and cannot be paged, so a full page means the
    bank may hold more than was exported.
    """
    result: RecallResult = await recall_fn(
        RecallRequest(
            query="*",
            bank_id=bank_id,
            max_results=batch_size,
        )
    )
    if len(result.hits) >= batch_size:
        logger.warning(
            "export_bank: provider has no listing API; recall returned a full page of %d "
            "for bank %r, so the export may be incomplete",
            batch_size,
            bank_id,
        )

    records: list[dict] = []
    seen: set[str] = set()
    for hit in result.hits:
        key = hit.memory_id or hit.text
        if key in seen:
            continue
        seen.add(key)
        records.append(
            _ama_record(
                memory_id=hit.memory_id or "",
                text=hit.text,
                fact_type=hit.fact_type,
                tags=hit.tags,
                metadata=hit.metadata,
                occurred_at=hit.occurred_at,
                source=hit.source,
                bank_id=hit.bank_id,
            )
        )
    return records


def _ama_record(
    *,
    memory_id: str,
    text: str,
    fact_type: str | None,
    tags: list[str] | None,
    metadata: Metadata | None,
    occurred_at: datetime | None,
    source: str | None,
    bank_id: str | None,
) -> dict:
    """Build one AMA memory line. Optional fields are omitted when empty."""
    record: dict = {"id": memory_id, "text": text}
    if fact_type:
        record["fact_type"] = fact_type
    if tags:
        record["tags"] = tags
    if metadata:
        record["metadata"] = metadata
    if occurred_at:
        record["occurred_at"] = occurred_at.isoformat()
    if source:
        record["source"] = source
    if bank_id:
        record["bank_id"] = bank_id
    # Embeddings and entities would come from provider-specific data;
    # Phase 1 exports only the fields above.
    return record


# ---------------------------------------------------------------------------
# Reader — iterate AMA JSONL lines
# ---------------------------------------------------------------------------


def read_ama_header(
    path: str | Path,
    *,
    allowed_roots: list[str | Path] | None = None,
    allow_uncontained: bool = False,
) -> AmaHeader:
    """Read and validate the AMA header (first line).

    See ``export_bank`` for ``allowed_roots`` and ``allow_uncontained`` semantics.
    """
    path = _safe_resolve(path, allowed_roots=allowed_roots, allow_uncontained=allow_uncontained)
    with open(path, encoding="utf-8") as f:
        first_line = f.readline().strip()
    if not first_line:
        raise ValueError(f"AMA file is empty: {path}")
    data = json.loads(first_line)
    if "_ama_version" not in data:
        raise ValueError(f"Not a valid AMA file (missing _ama_version): {path}")
    if data["_ama_version"] != AMA_VERSION:
        raise ValueError(f"Unsupported AMA version {data['_ama_version']} (expected {AMA_VERSION})")
    # Validate required field types
    for field in ("bank_id", "exported_at", "provider"):
        if not isinstance(data.get(field), str):
            raise ValueError(f"AMA header field '{field}' must be a string: {path}")
    if not isinstance(data.get("memory_count"), int):
        raise ValueError(f"AMA header field 'memory_count' must be an integer: {path}")
    return AmaHeader(
        bank_id=data["bank_id"],
        exported_at=data["exported_at"],
        provider=data["provider"],
        memory_count=data["memory_count"],
        _ama_version=data["_ama_version"],
    )


def iter_ama_memories(
    path: str | Path,
    *,
    allowed_roots: list[str | Path] | None = None,
    allow_uncontained: bool = False,
) -> list[AmaMemory]:
    """Read all memory records from an AMA file (skips header).

    See ``export_bank`` for ``allowed_roots`` and ``allow_uncontained`` semantics.
    """
    path = _safe_resolve(path, allowed_roots=allowed_roots, allow_uncontained=allow_uncontained)
    memories: list[AmaMemory] = []
    with open(path, encoding="utf-8") as f:
        # Skip header
        f.readline()
        for line_num, line in enumerate(f, start=2):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                if not isinstance(data, dict) or "id" not in data or "text" not in data:
                    logger.warning("AMA line %d: missing required fields (id, text)", line_num)
                    continue
                memories.append(
                    AmaMemory(
                        id=data["id"],
                        text=data["text"],
                        fact_type=data.get("fact_type"),
                        tags=data.get("tags"),
                        metadata=data.get("metadata"),
                        occurred_at=data.get("occurred_at"),
                        created_at=data.get("created_at"),
                        source=data.get("source"),
                        bank_id=data.get("bank_id"),
                        entities=data.get("entities"),
                        embedding=data.get("embedding"),
                    )
                )
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                logger.warning("AMA line %d: %s", line_num, exc)
                continue
    return memories


# ---------------------------------------------------------------------------
# Import — load AMA into a bank
# ---------------------------------------------------------------------------


@dataclass
class ImportResult:
    imported: int
    skipped: int
    errors: int


async def import_bank(
    retain_fn,
    bank_id: str,
    path: str | Path,
    on_conflict: Literal["skip", "overwrite", "error"] = "skip",
    progress_fn=None,
    *,
    allowed_roots: list[str | Path] | None = None,
    allow_uncontained: bool = False,
) -> ImportResult:
    """Import memories from an AMA file into a bank.

    Args:
        retain_fn: Async callable that takes a RetainRequest and returns RetainResult.
                   Typically ``brain._do_retain``.
        bank_id: Target bank (may differ from source bank in AMA).
        path: Path to AMA JSONL file.
        on_conflict: How to handle memories with IDs that already exist.
        progress_fn: Optional callback(imported, total) for progress reporting.
        allowed_roots: See ``export_bank``.
        allow_uncontained: See ``export_bank``.

    Returns:
        ImportResult with counts.
    """
    header = read_ama_header(path, allowed_roots=allowed_roots, allow_uncontained=allow_uncontained)
    memories = iter_ama_memories(path, allowed_roots=allowed_roots, allow_uncontained=allow_uncontained)

    imported = 0
    skipped = 0
    errors = 0

    for i, mem in enumerate(memories):
        try:
            # Parse occurred_at if present
            occurred_at = None
            if mem.occurred_at:
                try:
                    occurred_at = datetime.fromisoformat(mem.occurred_at)
                except ValueError:
                    logger.debug("Skipping unparseable occurred_at: %s", mem.occurred_at)

            request = RetainRequest(
                content=mem.text,
                bank_id=bank_id,
                metadata=mem.metadata,
                tags=mem.tags,
                occurred_at=occurred_at,
                source=mem.source or f"import:ama:{header.provider}",
                content_type="text",
            )

            result = await retain_fn(request)

            if result.stored:
                imported += 1
            elif result.deduplicated and on_conflict == "skip":
                skipped += 1
            elif result.deduplicated and on_conflict == "error":
                errors += 1
            else:
                skipped += 1

        except Exception as exc:
            logger.warning("AMA import line %d failed: %s", i + 2, exc)
            errors += 1

        if progress_fn and (i + 1) % 10 == 0:
            progress_fn(imported, header.memory_count)

    return ImportResult(imported=imported, skipped=skipped, errors=errors)
