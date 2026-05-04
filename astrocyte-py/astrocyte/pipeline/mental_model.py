"""Mental model service — first-class facade over :class:`MentalModelStore`.

Curated, refreshable saved-reflect summaries — the durable artifacts that
outlive any single recall and serve as the authoritative summary when the
recall pipeline elects to use the compiled layer.

History
-------

This service originally piggybacked on :class:`WikiStore`, distinguishing
mental models from wiki pages by ``kind="concept"`` and a
``metadata["_mental_model"] = True`` discriminator. That pattern overloaded
the wiki layer's lifecycle (revisions, lint issues, cross_links) for a
fundamentally different concept and required undocumented metadata-key
conventions. v1.x cut to a dedicated :class:`MentalModelStore` SPI with its
own table — this service now takes a store via dependency injection and is
agnostic to the underlying implementation.

The :class:`MentalModel` dataclass lives in :mod:`astrocyte.types` so the
SPI and consumer modules share a single source of truth; it's re-exported
from this module for backward compatibility with callers that import it
from :mod:`astrocyte.pipeline.mental_model`.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

# Re-export so existing ``from astrocyte.pipeline.mental_model import MentalModel``
# imports keep working after the dataclass moved to ``astrocyte.types``.
from astrocyte.types import MentalModel

if TYPE_CHECKING:
    from astrocyte.provider import MentalModelStore

__all__ = ["MentalModel", "MentalModelService"]


class MentalModelService:
    """First-class facade over a :class:`MentalModelStore`.

    All persistence behaviour (revision bumping, history archival, soft-
    delete, scope filtering) is delegated to the configured store. This
    service is the user-facing API surface — gateway endpoints and any
    in-process caller should use it rather than invoking the store
    directly.

    The store can be any :class:`~astrocyte.provider.MentalModelStore`
    implementation: ``InMemoryMentalModelStore`` for tests,
    ``PostgresMentalModelStore`` for production.
    """

    def __init__(self, store: "MentalModelStore") -> None:
        self._store = store

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
        """Create or refresh a mental model.

        Upsert semantics — calling create with an existing ``model_id``
        bumps the revision and archives the prior version. Use
        :meth:`refresh` when you specifically want to fail on missing.
        """
        # ``revision`` and ``refreshed_at`` are placeholders the store
        # overwrites — pass anything valid here.
        draft = MentalModel(
            model_id=model_id,
            bank_id=bank_id,
            title=title,
            content=content,
            scope=scope,
            source_ids=list(source_ids or []),
            revision=0,
            refreshed_at=datetime.now(UTC),
        )
        await self._store.upsert(draft, bank_id)
        stored = await self._store.get(model_id, bank_id)
        # If the store implementation returns ``None`` between upsert and
        # get (e.g. eventual consistency, hypothetical race), fall back to
        # the draft we sent — at least the caller sees the data they wrote.
        return stored or draft

    async def list(
        self,
        bank_id: str,
        *,
        scope: str | None = None,
    ) -> list[MentalModel]:
        return await self._store.list(bank_id, scope=scope)

    async def get(self, bank_id: str, model_id: str) -> MentalModel | None:
        return await self._store.get(model_id, bank_id)

    async def refresh(
        self,
        *,
        bank_id: str,
        model_id: str,
        content: str,
        source_ids: list[str] | None = None,
    ) -> MentalModel | None:
        """Refresh an EXISTING mental model. Returns ``None`` if missing.

        Distinct from :meth:`create` in that it requires the model to
        already exist — protects against silent creation on typo'd IDs.
        Source IDs default to the existing set (use :meth:`create` to
        replace with an empty list).
        """
        existing = await self._store.get(model_id, bank_id)
        if existing is None:
            return None
        next_sources = (
            list(source_ids) if source_ids is not None else list(existing.source_ids)
        )
        draft = replace(
            existing,
            content=content,
            source_ids=next_sources,
            refreshed_at=datetime.now(UTC),
        )
        await self._store.upsert(draft, bank_id)
        return await self._store.get(model_id, bank_id)

    async def delete(self, bank_id: str, model_id: str) -> bool:
        return await self._store.delete(model_id, bank_id)
