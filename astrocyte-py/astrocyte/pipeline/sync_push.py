"""Team-memory batch push: one stored row per client record (team-memory.md §8, G2).

``Astrocyte.push_records`` runs the policy layer (validation, PII, metadata,
provenance, rate limits) and hands the surviving records here. Each becomes
exactly one row with the client's id: no chunking, no extraction, no LLM call;
the text is re-embedded with this server's model.

Ids are global in the stores (both SQL stores key rows on ``id`` alone), so a
pushed id is checked against every bank before anything is written, and the
write itself is insert-only (``VectorStore.insert_vectors``): a row in another
bank, a forgotten row, or a row a concurrent push wrote first is never
overwritten.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from astrocyte.pipeline.embedding import generate_embeddings
from astrocyte.types import MemoryChange, SyncPushRecord, SyncPushResult, VectorItem

_logger = logging.getLogger("astrocyte.sync")

#: Same wording whether the id is held in another bank or in this one with
#: other text, so a push can't be used to learn that another bank holds an id.
REASON_IN_USE = "id is already in use with different text; memory text is immutable"
REASON_FORGOTTEN = "id was forgotten; a forgotten memory can't be pushed again"
REASON_METADATA_IGNORED = "text unchanged; metadata updates are not accepted by push"


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def classify_existing(bank_id: str, record: SyncPushRecord, found: MemoryChange) -> SyncPushResult:
    """The result for a pushed id the store already holds.

    Text is immutable; other fields (metadata, and later claim status, trust,
    staleness) will change through their own update path, so a re-push with
    the same text but different metadata is ``unchanged`` and the stored
    metadata is left as it is.
    """
    if found.bank_id != bank_id:
        return SyncPushResult(id=record.id, status="rejected", reason=REASON_IN_USE)
    if found.deleted:
        return SyncPushResult(id=record.id, status="rejected", reason=REASON_FORGOTTEN)
    if found.text != record.text:
        return SyncPushResult(id=record.id, status="rejected", reason=REASON_IN_USE)
    same_fields = (
        (found.metadata or None) == (record.metadata or None)
        and (found.tags or None) == (record.tags or None)
        and found.fact_type == record.fact_type
        and _utc(found.occurred_at) == _utc(record.occurred_at)
    )
    return SyncPushResult(
        id=record.id,
        status="unchanged",
        reason=None if same_fields else REASON_METADATA_IGNORED,
    )


class SyncPushStageMixin:
    """``push_records`` for :class:`~astrocyte.pipeline.orchestrator.PipelineOrchestrator`."""

    async def push_records(self: Any, bank_id: str, records: list[SyncPushRecord]) -> list[SyncPushResult]:
        """Store each record as one row with its own id; see the module docstring.

        ``records`` have already passed the policy layer (their text is the
        possibly-redacted text, their metadata sanitized and stamped). The
        vector store must have ``lookup_ids`` and ``insert_vectors``. Returns
        one result per record, in order.
        """
        store = self.vector_store
        existing = {c.id: c for c in await store.lookup_ids([r.id for r in records])}
        results: list[SyncPushResult | None] = [None] * len(records)
        pending: list[int] = []
        for i, record in enumerate(records):
            found = existing.get(record.id)
            if found is None:
                pending.append(i)
            else:
                results[i] = classify_existing(bank_id, record, found)

        embeddings = await generate_embeddings([records[i].text for i in pending], self.llm_provider) if pending else []
        stored: list[VectorItem] = []
        warn_only = self._config_dedup_action(bank_id) == "warn"
        for i, embedding in zip(pending, embeddings):
            record = records[i]
            # One at a time, so a record near-duplicating one stored earlier in
            # this batch is caught (the dedup cache learns each insert).
            [duplicate_of] = await self._find_duplicate_ids(bank_id, [record.text], [embedding], None)
            if duplicate_of is not None and duplicate_of != record.id and not warn_only:
                results[i] = SyncPushResult(id=record.id, status="duplicate", duplicate_of=duplicate_of)
                continue
            item = VectorItem(
                id=record.id,
                bank_id=bank_id,
                vector=embedding,
                text=record.text,
                metadata=record.metadata,
                tags=record.tags,
                fact_type=record.fact_type,
                occurred_at=record.occurred_at,
                retained_at=datetime.now(timezone.utc),
            )
            if await store.insert_vectors([item]):
                results[i] = SyncPushResult(id=record.id, status="stored")
                self._dedup.add(bank_id, record.id, embedding, text=record.text)
                stored.append(item)
                continue
            # Someone else holds the id now: the same id earlier in this
            # batch, or a concurrent push. Answer as if it had been there.
            [*now_found] = await store.lookup_ids([record.id])
            results[i] = (
                classify_existing(bank_id, record, now_found[0])
                if now_found
                else SyncPushResult(id=record.id, status="rejected", reason="could not be stored")
            )

        await self._mirror_pushed(bank_id, stored)
        return [r for r in results if r is not None]

    async def _mirror_pushed(self: Any, bank_id: str, items: list[VectorItem]) -> None:
        """What retain does after storing vectors, minus anything that calls
        an LLM: the keyword index and the semantic-kNN graph. Best-effort."""
        if not items:
            return
        if self.document_store is not None:
            from astrocyte.types import Document

            for item in items:
                try:
                    await self.document_store.store_document(
                        Document(id=item.id, text=item.text, metadata=item.metadata, tags=item.tags), bank_id
                    )
                except Exception as exc:
                    _logger.warning("sync push: document_store.store_document failed for %s: %s", item.id, exc)
        await self._persist_semantic_links(bank_id, [i.id for i in items], [i.vector for i in items])
