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
        kind: str = "general",
        structured_doc: dict | None = None,
        source_timestamps: list[datetime] | None = None,
    ) -> MentalModel:
        """Create or refresh a mental model.

        Upsert semantics — calling create with an existing ``model_id``
        bumps the revision and archives the prior version. Use
        :meth:`refresh` when you specifically want to fail on missing.

        ``kind`` and ``structured_doc`` (M21) let callers populate the
        sub-type discriminator and the typed-document representation
        in the single initial upsert — avoids a double-bump when
        creating ``directive``-kind models or sections-shaped docs.

        ``source_timestamps`` (M40) is an optional list of per-source
        evidence timestamps, positionally aligned with ``source_ids``.
        When omitted, the store persists ``NULL`` and trend computation
        downstream falls back to ``refreshed_at`` as a single-point
        anchor. When provided, must equal ``len(source_ids)`` in length
        (the store drops mismatched arrays rather than persist a
        wrong-alignment record).
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
            kind=kind,
            structured_doc=structured_doc,
            source_timestamps=source_timestamps,
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
        next_sources = list(source_ids) if source_ids is not None else list(existing.source_ids)
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

    async def update_via_ops(
        self,
        *,
        bank_id: str,
        model_id: str,
        operations: list[dict],
    ) -> "tuple[MentalModel, dict] | None":
        """M21 — apply structured delta operations to a mental model.

        Thin wrapper around
        :meth:`astrocyte.provider.MentalModelStore.update_via_ops`
        that re-fetches the post-update model for the caller. Returns
        ``None`` when the model doesn't exist; otherwise
        ``(updated_model, applied_delta_summary)``.

        ``applied_delta_summary`` is the audit trail from
        :class:`~astrocyte.pipeline.delta_ops.AppliedDelta` —
        ``{"applied": [...], "skipped": [...], "changed": bool}``
        — exposed so callers (incl. the MCP tool) can surface which
        ops landed and which the validator dropped.
        """
        result = await self._store.update_via_ops(model_id, bank_id, operations)
        if result is None:
            return None
        _new_revision, summary = result
        updated = await self._store.get(model_id, bank_id)
        if updated is None:
            return None
        return (updated, summary)

    async def refresh_from_sources(
        self,
        *,
        bank_id: str,
        model_id: str,
        new_source_ids: list[str],
    ) -> MentalModel | None:
        """M28 — re-derive a model from an extended source set.

        Thin wrapper around
        :meth:`astrocyte.provider.MentalModelStore.refresh` that first
        verifies the model exists (so the caller can distinguish
        "missing model" from "store refresh returned None"). Returns
        the refreshed :class:`MentalModel` or ``None`` if the model
        doesn't exist.

        Hindsight parity: this is the per-model analogue of the
        ``mental_model_compile`` pipeline — after retain adds new
        memories that pertain to an existing model, this re-runs the
        compile against the extended ``source_ids`` and bumps the
        revision.
        """
        existing = await self._store.get(model_id, bank_id)
        if existing is None:
            return None
        return await self._store.refresh(model_id, bank_id, list(new_source_ids))
