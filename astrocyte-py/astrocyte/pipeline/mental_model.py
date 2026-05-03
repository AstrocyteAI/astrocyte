"""Mental model service backed by the existing WikiStore abstraction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from astrocyte.types import WikiPage


@dataclass(frozen=True)
class MentalModel:
    model_id: str
    bank_id: str
    title: str
    content: str
    scope: str
    source_ids: list[str]
    revision: int
    refreshed_at: datetime


class MentalModelService:
    """First-class facade for curated saved-reflect summaries.

    The storage substrate is WikiStore so Astrocyte does not fork another
    persistence model; mental models are distinguished by ``kind="concept"``
    and ``metadata["_mental_model"] = True``.
    """

    def __init__(self, wiki_store) -> None:
        self._wiki_store = wiki_store

    async def create(
        self,
        *,
        bank_id: str,
        model_id: str,
        title: str,
        content: str,
        scope: str = "bank",
        source_ids: list[str] | None = None,
    ) -> MentalModel:
        page = WikiPage(
            page_id=model_id,
            bank_id=bank_id,
            kind="concept",
            title=title,
            content=content,
            scope=scope,
            source_ids=list(source_ids or []),
            cross_links=[],
            revision=1,
            revised_at=datetime.now(UTC),
            metadata={"_mental_model": True, "_freshness": "fresh"},
        )
        await self._wiki_store.upsert_page(page, bank_id)
        stored = await self._wiki_store.get_page(model_id, bank_id)
        return _from_page(stored or page)

    async def list(self, bank_id: str, *, scope: str | None = None) -> list[MentalModel]:
        pages = await self._wiki_store.list_pages(bank_id, scope=scope, kind="concept")
        return [_from_page(page) for page in pages if (page.metadata or {}).get("_mental_model") is True]

    async def get(self, bank_id: str, model_id: str) -> MentalModel | None:
        page = await self._wiki_store.get_page(model_id, bank_id)
        if page is None or (page.metadata or {}).get("_mental_model") is not True:
            return None
        return _from_page(page)

    async def refresh(
        self,
        *,
        bank_id: str,
        model_id: str,
        content: str,
        source_ids: list[str] | None = None,
    ) -> MentalModel | None:
        page = await self._wiki_store.get_page(model_id, bank_id)
        if page is None or (page.metadata or {}).get("_mental_model") is not True:
            return None
        refreshed = WikiPage(
            page_id=page.page_id,
            bank_id=page.bank_id,
            kind=page.kind,
            title=page.title,
            content=content,
            scope=page.scope,
            source_ids=list(source_ids if source_ids is not None else page.source_ids),
            cross_links=page.cross_links,
            revision=page.revision,
            revised_at=datetime.now(UTC),
            tags=page.tags,
            metadata={**(page.metadata or {}), "_freshness": "fresh"},
        )
        await self._wiki_store.upsert_page(refreshed, bank_id)
        stored = await self._wiki_store.get_page(model_id, bank_id)
        return _from_page(stored or refreshed)

    async def delete(self, bank_id: str, model_id: str) -> bool:
        return bool(await self._wiki_store.delete_page(model_id, bank_id))


def _from_page(page: WikiPage) -> MentalModel:
    return MentalModel(
        model_id=page.page_id,
        bank_id=page.bank_id,
        title=page.title,
        content=page.content,
        scope=page.scope,
        source_ids=page.source_ids,
        revision=page.revision,
        refreshed_at=page.revised_at,
    )
