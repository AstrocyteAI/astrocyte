"""Async Elasticsearch implementation of :class:`~astrocyte.provider.DocumentStore`."""

from __future__ import annotations

from typing import ClassVar

from astrocyte.types import Document, DocumentFilters, DocumentHit, HealthStatus
from elasticsearch import AsyncElasticsearch, NotFoundError


def _norm_score(raw: float, max_score: float) -> float:
    if max_score <= 0:
        return 0.0
    return max(0.0, min(1.0, float(raw) / max_score))


class ElasticsearchDocumentStore:
    """One index per bank: ``{index_prefix}_{bank_id}`` (sanitized)."""

    SPI_VERSION: ClassVar[int] = 1

    def __init__(self, url: str, *, index_prefix: str = "astrocyte_docs") -> None:
        self._es = AsyncElasticsearch(url)
        self._prefix = index_prefix

    def _index(self, bank_id: str) -> str:
        safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in bank_id.lower())
        return f"{self._prefix}_{safe}"

    async def store_document(self, document: Document, bank_id: str) -> str:
        idx = self._index(bank_id)
        body: dict[str, object] = {
            "text": document.text,
            "tags": list(document.tags or []),
        }
        if document.metadata:
            body["metadata"] = dict(document.metadata)
        await self._es.index(index=idx, id=document.id, document=body, refresh="wait_for")
        return document.id

    async def search_fulltext(
        self,
        query: str,
        bank_id: str,
        limit: int = 10,
        filters: DocumentFilters | None = None,
    ) -> list[DocumentHit]:
        idx = self._index(bank_id)
        must: list[dict[str, object]] = [{"match": {"text": {"query": query}}}]
        if filters and filters.tags:
            must.append({"terms": {"tags": list(filters.tags)}})
        resp = await self._es.search(
            index=idx,
            query={"bool": {"must": must}},
            size=limit,
        )
        hits = resp.get("hits", {}).get("hits", [])
        max_score = float(resp.get("hits", {}).get("max_score") or 0.0) or 1.0
        out: list[DocumentHit] = []
        for h in hits:
            src = h.get("_source", {}) or {}
            raw = float(h.get("_score") or 0.0)
            mid = str(h.get("_id") or "")
            meta = src.get("metadata")
            out.append(
                DocumentHit(
                    document_id=mid,
                    text=str(src.get("text") or ""),
                    score=_norm_score(raw, max_score),
                    metadata=meta if isinstance(meta, dict) else None,
                ),
            )
        return out

    async def get_document(self, document_id: str, bank_id: str) -> Document | None:
        idx = self._index(bank_id)
        try:
            r = await self._es.get(index=idx, id=document_id)
        except NotFoundError:
            return None
        src = r.get("_source", {}) or {}
        return Document(
            id=document_id,
            text=str(src.get("text") or ""),
            metadata=src.get("metadata") if isinstance(src.get("metadata"), dict) else None,
            tags=list(src.get("tags") or []) if src.get("tags") is not None else None,
        )

    async def health(self) -> HealthStatus:
        try:
            await self._es.info()
            return HealthStatus(healthy=True, message="elasticsearch ok")
        except Exception as e:
            return HealthStatus(healthy=False, message=str(e))
