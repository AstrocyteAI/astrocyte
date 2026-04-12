"""Attach :class:`~astrocyte._astrocyte.Astrocyte` to ingest :func:`~astrocyte.ingest.webhook.RetainCallable` for stream sources."""

from __future__ import annotations

from typing import TYPE_CHECKING

from astrocyte.types import AstrocyteContext, RetainResult

if TYPE_CHECKING:
    from astrocyte._astrocyte import Astrocyte

from astrocyte.ingest.webhook import RetainCallable


def retain_callable_for_astrocyte(astrocyte: Astrocyte) -> RetainCallable:
    """Build a ``retain`` async callable suitable for :class:`SourceRegistry.from_sources_config` (``retain=``)."""

    async def retain(
        text: str,
        bank_id: str,
        *,
        metadata: dict[str, str | int | float | bool | None] | None = None,
        content_type: str = "text",
        extraction_profile: str | None = None,
        source: str | None = None,
        principal: str | None = None,
        context: AstrocyteContext | None = None,
    ) -> RetainResult:
        ctx = context
        if ctx is None and principal:
            ctx = AstrocyteContext(principal=principal)
        return await astrocyte.retain(
            text,
            bank_id,
            metadata=metadata,
            content_type=content_type,
            extraction_profile=extraction_profile,
            source=source,
            context=ctx,
        )

    return retain
