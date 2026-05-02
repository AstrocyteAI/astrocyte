"""Memory portability — AMA (Astrocyte Memory Archive) export and import.

AMA is a newline-delimited JSON (JSONL) format. Line 1 is the header,
subsequent lines are individual memories. Streamable, self-describing,
and FFI-safe (plain JSON, no Python-specific types).

See docs/_design/memory-portability.md for the full specification.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from astrocyte.types import MemoryHit, Metadata, RecallRequest, RecallResult, RetainRequest

logger = logging.getLogger("astrocyte.portability")

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

    Returns:
        Number of memories exported.
    """
    path = Path(path).resolve()
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


def read_ama_header(path: str | Path) -> AmaHeader:
    """Read and validate the AMA header (first line)."""
    path = Path(path).resolve()
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


def iter_ama_memories(path: str | Path) -> list[AmaMemory]:
    """Read all memory records from an AMA file (skips header)."""
    path = Path(path).resolve()
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
) -> ImportResult:
    """Import memories from an AMA file into a bank.

    Args:
        retain_fn: Async callable that takes a RetainRequest and returns RetainResult.
                   Typically ``brain._do_retain``.
        bank_id: Target bank (may differ from source bank in AMA).
        path: Path to AMA JSONL file.
        on_conflict: How to handle memories with IDs that already exist.
        progress_fn: Optional callback(imported, total) for progress reporting.

    Returns:
        ImportResult with counts.
    """
    header = read_ama_header(path)
    memories = iter_ama_memories(path)

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
