"""Async Qdrant implementation of :class:`~astrocyte.provider.VectorStore`."""

from __future__ import annotations

import json
import uuid
from typing import ClassVar

from astrocyte.types import HealthStatus, VectorFilters, VectorHit, VectorItem
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    PointStruct,
    VectorParams,
)

_BANK = "bank_id"
_MEMORY = "memory_id"


def _point_uuid(memory_id: str, bank_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"astrocyte:{bank_id}:{memory_id}"))


def _score_to_unit(raw: float) -> float:
    """Qdrant cosine similarity scores are typically in [0, 1]; clamp defensively."""
    if raw != raw:  # NaN
        return 0.0
    return max(0.0, min(1.0, float(raw)))


class QdrantVectorStore:
    """Vector store backed by a single Qdrant collection (bank isolation via payload filter)."""

    SPI_VERSION: ClassVar[int] = 1

    def __init__(
        self,
        url: str,
        collection_name: str,
        vector_size: int,
        *,
        api_key: str | None = None,
        timeout: float | None = 30.0,
    ) -> None:
        self._collection = collection_name
        self._vector_size = vector_size
        kwargs: dict[str, object] = {"url": url, "timeout": timeout}
        if api_key:
            kwargs["api_key"] = api_key
        self._client = AsyncQdrantClient(**kwargs)

    async def _ensure_collection(self) -> None:
        cols = await self._client.get_collections()
        names = {c.name for c in cols.collections}
        if self._collection not in names:
            await self._client.create_collection(
                collection_name=self._collection,
                vectors_config=VectorParams(size=self._vector_size, distance=Distance.COSINE),
            )

    def _bank_filter(self, bank_id: str) -> Filter:
        return Filter(must=[FieldCondition(key=_BANK, match=MatchValue(value=bank_id))])

    def _filters(self, bank_id: str, filters: VectorFilters | None) -> Filter:
        must: list[FieldCondition] = [FieldCondition(key=_BANK, match=MatchValue(value=bank_id))]
        if filters:
            if filters.tags:
                must.append(FieldCondition(key="tags", match=MatchAny(any=list(filters.tags))))
            # fact_types: optional OR filter — omitted in v1 (payload uses a single string field)
        return Filter(must=must)

    async def store_vectors(self, items: list[VectorItem]) -> list[str]:
        if not items:
            return []
        await self._ensure_collection()
        wrong = [i for i in items if len(i.vector) != self._vector_size]
        if wrong:
            raise ValueError(
                f"Vector dimension mismatch: expected {self._vector_size}, got {len(wrong[0].vector)}",
            )
        points: list[PointStruct] = []
        for item in items:
            pid = _point_uuid(item.id, item.bank_id)
            meta_payload: dict[str, object] = {}
            if item.metadata:
                meta_payload["meta_json"] = json.dumps(item.metadata, sort_keys=True)
            payload: dict[str, object] = {
                _BANK: item.bank_id,
                _MEMORY: item.id,
                "text": item.text,
                "tags": list(item.tags or []),
                "fact_type": item.fact_type,
                "memory_layer": item.memory_layer,
                "occurred_at": item.occurred_at.isoformat() if item.occurred_at else None,
                **meta_payload,
            }
            points.append(
                PointStruct(
                    id=pid,
                    vector=item.vector,
                    payload=payload,
                ),
            )
        await self._client.upsert(collection_name=self._collection, points=points, wait=True)
        return [i.id for i in items]

    async def search_similar(
        self,
        query_vector: list[float],
        bank_id: str,
        limit: int = 10,
        filters: VectorFilters | None = None,
    ) -> list[VectorHit]:
        if len(query_vector) != self._vector_size:
            raise ValueError(
                f"Query vector dimension mismatch: expected {self._vector_size}, got {len(query_vector)}",
            )
        await self._ensure_collection()
        flt = self._filters(bank_id, filters)
        res = await self._client.query_points(
            collection_name=self._collection,
            query=query_vector,
            query_filter=flt,
            limit=limit,
            with_payload=True,
        )
        hits: list[VectorHit] = []
        for sp in res.points:
            pl = sp.payload or {}
            mid = str(pl.get(_MEMORY) or "")
            text = str(pl.get("text") or "")
            meta = None
            mj = pl.get("meta_json")
            if isinstance(mj, str) and mj.strip():
                try:
                    meta = json.loads(mj)
                except json.JSONDecodeError:
                    meta = None
            hits.append(
                VectorHit(
                    id=mid,
                    text=text,
                    score=_score_to_unit(sp.score),
                    metadata=meta,
                    tags=list(pl.get("tags") or []) if pl.get("tags") is not None else None,
                    fact_type=str(pl["fact_type"]) if pl.get("fact_type") else None,
                    memory_layer=str(pl["memory_layer"]) if pl.get("memory_layer") else None,
                ),
            )
        return hits

    async def delete(self, ids: list[str], bank_id: str) -> int:
        if not ids:
            return 0
        await self._ensure_collection()
        pids = [_point_uuid(i, bank_id) for i in ids]
        await self._client.delete(
            collection_name=self._collection,
            points_selector=pids,
            wait=True,
        )
        return len(ids)

    async def list_vectors(
        self,
        bank_id: str,
        offset: int = 0,
        limit: int = 100,
    ) -> list[VectorItem]:
        await self._ensure_collection()
        flt = self._bank_filter(bank_id)
        out: list[VectorItem] = []
        next_offset = None
        while len(out) < offset + limit:
            points, next_offset = await self._client.scroll(
                collection_name=self._collection,
                scroll_filter=flt,
                limit=256,
                offset=next_offset,
                with_vectors=True,
                with_payload=True,
            )
            if not points:
                break
            for p in points:
                pl = p.payload or {}
                mid = str(pl.get(_MEMORY) or "")
                vec = p.vector
                if isinstance(vec, dict):
                    continue
                meta = None
                mj = pl.get("meta_json")
                if isinstance(mj, str) and mj.strip():
                    try:
                        meta = json.loads(mj)
                    except json.JSONDecodeError:
                        meta = None
                out.append(
                    VectorItem(
                        id=mid,
                        bank_id=bank_id,
                        vector=list(vec) if vec is not None else [],
                        text=str(pl.get("text") or ""),
                        metadata=meta,
                        tags=list(pl.get("tags") or []) if pl.get("tags") is not None else None,
                        fact_type=str(pl["fact_type"]) if pl.get("fact_type") else None,
                        memory_layer=str(pl["memory_layer"]) if pl.get("memory_layer") else None,
                    ),
                )
            if next_offset is None:
                break
        out.sort(key=lambda x: x.id)
        return out[offset : offset + limit]

    async def health(self) -> HealthStatus:
        try:
            await self._client.get_collections()
            return HealthStatus(healthy=True, message="qdrant ok")
        except Exception as e:
            return HealthStatus(healthy=False, message=str(e))
