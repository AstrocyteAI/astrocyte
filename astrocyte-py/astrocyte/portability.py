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
from typing import Literal

from astrocyte.types import MemoryHit, Metadata, RecallRequest, RecallResult, RetainRequest

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


def _safe_resolve(
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
        raise ValueError(
            f"Portability path contains illegal control character: {path_str!r}"
        )
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
    raise ValueError(
        f"Portability path escapes allowed roots: {resolved!s} "
        f"not in {[str(r) for r in roots]}"
    )


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
    recall_fn,
    bank_id: str,
    path: str | Path,
    provider_name: str = "unknown",
    include_embeddings: bool = False,
    include_entities: bool = True,
    batch_size: int = 100,
    *,
    allowed_roots: list[str | Path] | None = None,
    allow_uncontained: bool = False,
) -> int:
    """Export a memory bank to AMA JSONL format.

    Args:
        recall_fn: Async callable that takes a RecallRequest and returns RecallResult.
                   Typically ``brain._do_recall``.
        bank_id: Bank to export.
        path: Output file path.
        provider_name: Provider identifier for the header.
        include_embeddings: Include vector embeddings (not portable across models).
        include_entities: Include extracted entities.
        batch_size: Number of memories per recall batch.
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
    path = _safe_resolve(path, allowed_roots=allowed_roots, allow_uncontained=allow_uncontained)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Collect all memories via recall with large limit
    all_hits: list[MemoryHit] = []
    offset = 0
    while True:
        result: RecallResult = await recall_fn(
            RecallRequest(
                query="*",  # Wildcard — retrieve everything
                bank_id=bank_id,
                max_results=batch_size,
            )
        )
        if not result.hits:
            break
        all_hits.extend(result.hits)
        # If we got fewer than batch_size, we've exhausted the bank
        if len(result.hits) < batch_size:
            break
        offset += batch_size
        # Safety: prevent infinite loops for providers that always return results
        if offset > 100000:
            break

    # Deduplicate by memory_id
    seen: set[str] = set()
    unique_hits: list[MemoryHit] = []
    for hit in all_hits:
        key = hit.memory_id or hit.text
        if key not in seen:
            seen.add(key)
            unique_hits.append(hit)

    # Write AMA file
    now = datetime.now(timezone.utc).isoformat()
    header = {
        "_ama_version": AMA_VERSION,
        "bank_id": bank_id,
        "exported_at": now,
        "provider": provider_name,
        "memory_count": len(unique_hits),
    }

    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(header, default=str) + "\n")
        for hit in unique_hits:
            record: dict = {
                "id": hit.memory_id or "",
                "text": hit.text,
            }
            if hit.fact_type:
                record["fact_type"] = hit.fact_type
            if hit.tags:
                record["tags"] = hit.tags
            if hit.metadata:
                record["metadata"] = hit.metadata
            if hit.occurred_at:
                record["occurred_at"] = hit.occurred_at.isoformat()
            if hit.source:
                record["source"] = hit.source
            if hit.bank_id:
                record["bank_id"] = hit.bank_id
            # Embeddings and entities would come from provider-specific data
            # For Phase 1, we export what's available in MemoryHit
            f.write(json.dumps(record, default=str) + "\n")

    return len(unique_hits)


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
