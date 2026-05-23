"""M32 — PageIndex recall pipeline (unifies bench + production stacks).

Implements the same ``async def recall(request: RecallRequest) -> RecallResult``
contract as the legacy ``orchestrator.recall()`` so it slots into the
existing ``ProviderDispatcher`` without changes to ``Astrocyte.recall()``.

Why this exists
---------------

Before M32, Astrocyte had two parallel retrieval stacks:

- **PageIndex** (bench): ``astrocyte_client.search()`` → ``fact_recall`` +
  ``section_recall`` + cross-encoder rerank → ``PostgresPageIndexStore``.
  Every bench score since M14 measured this stack. All the M14-M31 cycle
  work (RRF fusion, fact↔chunk pairing, per-Q-type prompts, M27 fields,
  M28-M29 coreference, M30 parallelization, M31 session_filter +
  event_date) lives here.
- **Vector store** (public ``Astrocyte.recall()``): orchestrator → vector
  store + graph store. M9-era plumbing; none of the M14-M31 improvements
  ever landed here.

The v0.15.0 ship audit surfaced the drift: README badges describe the
PageIndex stack but users calling ``Astrocyte.recall()`` get the
vector-store stack. M32 closes that gap by making PageIndex the
production recall pipeline, so future bench scores actually represent
what ``pip install astrocyte`` produces.

Design notes
------------

- **No new retrieval logic.** This pipeline is a thin adapter around
  the existing ``fact_recall`` + ``section_recall`` primitives + the
  bench-validated rerank. It's not a re-implementation; it's a re-shape
  of the result type.
- **Result-shape adapter.** Fact-grain and section-grain candidates
  become ``MemoryHit`` instances with ``memory_layer`` set so downstream
  consumers can tell them apart.
- **Honours ``RecallRequest`` fields** the bench has historically
  threaded through: ``session_id`` (M31 Fix 2), ``time_range`` (M9
  temporal filter), ``fact_types``, ``max_results``,
  ``query_reference_date``, ``as_of``. Multi-bank ``banks=[...]``
  routing stays on the legacy ``orchestrator.recall()`` for now —
  PageIndex is single-bank/single-document per call.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from astrocyte.types import (
    MemoryHit,
    RecallRequest,
    RecallResult,
    RecallTrace,
)

if TYPE_CHECKING:
    from astrocyte.config import AstrocyteConfig
    from astrocyte.provider import LLMProvider, PageIndexStore

_logger = logging.getLogger("astrocyte.pipeline.pageindex_pipeline")


class PageIndexPipeline:
    """Recall pipeline that drives the PageIndex stack.

    Implements the ``async recall(request) -> RecallResult`` contract
    so ``ProviderDispatcher`` treats it like any other pipeline.
    """

    def __init__(
        self,
        store: "PageIndexStore",
        embedding_provider: "LLMProvider",
        config: "AstrocyteConfig | None" = None,
        *,
        document_resolver: Any | None = None,
    ) -> None:
        """
        Args:
          store: PageIndex SPI handle (Postgres or in-memory).
          embedding_provider: For query-embedding the search text.
          config: Astrocyte config; used to gate optional retrieval
            features (episodic, link-expansion). Defaults to a fresh
            ``AstrocyteConfig()``.
          document_resolver: Optional callable mapping
            ``(bank_id) -> document_id | None``. Used when the caller
            wants single-document scope (matches the bench's
            ``self._doc_ids[user_id]`` pattern). When ``None``, the
            pipeline runs bank-wide (``document_id=None`` to the SPI).
        """
        if config is None:
            from astrocyte.config import AstrocyteConfig  # noqa: PLC0415

            config = AstrocyteConfig()
        self._store = store
        self._provider = embedding_provider
        self._config = config
        self._document_resolver = document_resolver

    async def recall(self, request: RecallRequest) -> RecallResult:
        """Run PageIndex retrieval + rerank, return RecallResult."""
        t0 = time.monotonic()

        # 1. Embed query.
        try:
            embeds = await self._provider.embed([request.query])
            query_vec = embeds[0] if embeds else []
        except Exception as exc:  # noqa: BLE001
            _logger.warning("pageindex_pipeline: embed failed: %s", exc)
            return RecallResult(hits=[], total_available=0, truncated=False)

        if not query_vec:
            return RecallResult(hits=[], total_available=0, truncated=False)

        # 2. Resolve optional document scope.
        document_id: str | None = None
        if self._document_resolver is not None and request.bank_id:
            try:
                document_id = self._document_resolver(request.bank_id)
            except Exception:  # noqa: BLE001
                document_id = None

        # 3. Query analyzer for temporal range (only when caller didn't
        # provide one). Resolves relative phrases like "last week" using
        # ``query_reference_date`` (or ``as_of``) as the anchor.
        date_range = request.time_range
        if date_range is None:
            try:
                from astrocyte.pipeline.query_analyzer import (  # noqa: PLC0415
                    analyze_query,
                )

                anchor = request.query_reference_date or request.as_of
                analysis = await analyze_query(
                    request.query,
                    reference_date=anchor,
                    llm_provider=None,
                    allow_llm_fallback=False,
                    allow_temporal_expansion=True,
                )
                if (
                    analysis.temporal_constraint
                    and analysis.temporal_constraint.is_bounded()
                ):
                    date_range = (
                        analysis.temporal_constraint.start_date,
                        analysis.temporal_constraint.end_date,
                    )
            except Exception:  # noqa: BLE001
                date_range = None

        # 4. Parallel fact + section recall (M30-L1 pattern).
        from astrocyte.pipeline.fact_recall import fact_recall  # noqa: PLC0415
        from astrocyte.pipeline.section_recall import (  # noqa: PLC0415
            section_recall,
        )

        fact_coro = fact_recall(
            store=self._store,
            bank_id=request.bank_id,
            document_id=document_id,
            query=request.query,
            query_embedding=query_vec,
            config=self._config,
            temporal_range=date_range,
            session_filter=request.session_id,  # M31 Fix 2
            # M34-4 — per-fact-type segmentation when caller specifies
            # which types to retrieve; default None preserves single-pool.
            fact_types=request.fact_types,
            # M35-2 — token budget cap. None → no cap (legacy
            # callers); otherwise tiktoken-counted pack from
            # token_budget.pack_to_budget.
            max_tokens=request.max_tokens,
        )

        recall_mode = "temporal" if date_range is not None else "single-hop"
        section_coro = section_recall(
            store=self._store,
            bank_id=request.bank_id,
            question=request.query,
            mode=recall_mode,
            embedding_provider=self._provider,
            date_range=date_range,
            wiki_enabled=False,  # Wiki tier handled by orchestrator's _try_wiki_tier
            session_filter=request.session_id,  # M31 Fix 2
        )

        fact_result, section_result = await asyncio.gather(
            fact_coro, section_coro, return_exceptions=True,
        )

        fact_hits: list = []
        if isinstance(fact_result, BaseException):
            _logger.warning(
                "pageindex_pipeline: fact_recall failed: %s", fact_result,
            )
        else:
            fact_hits = fact_result

        section_recall_result = None
        if isinstance(section_result, BaseException):
            _logger.warning(
                "pageindex_pipeline: section_recall failed: %s",
                section_result,
            )
        else:
            section_recall_result = section_result

        # 5. Convert to MemoryHit. fact_types filter is applied here so
        # downstream consumers see only the requested types.
        hits = self._to_memory_hits(
            fact_hits=fact_hits,
            section_result=section_recall_result,
            request=request,
        )

        # 6. Honour max_results truncation; report truncated flag.
        total = len(hits)
        truncated = total > request.max_results
        hits = hits[: request.max_results]

        elapsed_ms = (time.monotonic() - t0) * 1000.0
        trace = RecallTrace(
            strategies_used=["fact_recall", "section_recall"],
            total_candidates=total,
            fusion_method="rrf+rerank",
            latency_ms=elapsed_ms,
        )
        return RecallResult(
            hits=hits,
            total_available=total,
            truncated=truncated,
            trace=trace,
        )

    def _to_memory_hits(
        self,
        *,
        fact_hits: list,
        section_result: Any,
        request: RecallRequest,
    ) -> list[MemoryHit]:
        """Shape fact/section results into the public ``MemoryHit`` list.

        Fact-grain → ``memory_layer="fact"``; section-grain →
        ``memory_layer="section"``. M31 ``event_date`` is preferred over
        ``occurred_start`` for the ``occurred_at`` surface field, since
        ``event_date`` is the deterministically-resolved canonical date
        for the fact's primary event.
        """
        # Apply fact_types filter early.
        if request.fact_types:
            wanted = set(request.fact_types)
            fact_hits = [
                fh for fh in fact_hits if (fh.fact_type or "") in wanted
            ]

        out: list[MemoryHit] = []
        for fh in fact_hits:
            occurred = getattr(fh, "event_date", None) or fh.occurred_start
            # M32 — stash the fact-grain metadata (M27 confidence_score,
            # M27 mentioned_at, M31 Fix 4 event_date, line_num for
            # bench-side source-chunk rendering) into the metadata dict
            # so downstream consumers (bench harness, production agents)
            # can read them without adding fields to MemoryHit's public
            # surface. ``None`` values are still included so consumers
            # can do a single `hit.metadata.get("confidence_score")`
            # check without an attribute branch.
            meta: dict[str, Any] = {
                "grain": "fact",
                "confidence_score": getattr(fh, "confidence_score", None),
                "mentioned_at": getattr(fh, "mentioned_at", None),
                "event_date": getattr(fh, "event_date", None),
                "line_num": fh.line_num,
                "document_id": fh.document_id,
                "speaker": getattr(fh, "speaker", None),
                "entities": list(getattr(fh, "entities", None) or []),
            }
            out.append(
                MemoryHit(
                    text=fh.text,
                    score=fh.score,
                    fact_type=fh.fact_type,
                    metadata=meta,
                    occurred_at=occurred,
                    source=None,
                    memory_id=fh.fact_id,
                    bank_id=request.bank_id,
                    memory_layer="fact",
                    chunk_id=fh.chunk_id,
                )
            )

        # Section-grain: ``section_recall`` returns a ``SectionRecallResult``
        # with ``fused`` (FusedHit list) and ``wiki_hits``. We surface the
        # fused section hits as MemoryHit(memory_layer="section") so the
        # public API mirrors the bench's multi-grain shape. ``wiki_hits``
        # are skipped here — the orchestrator's _try_wiki_tier handles them
        # at a different layer.
        if section_result is not None and getattr(section_result, "fused", None):
            for sh in section_result.fused:
                title = getattr(sh, "title", None) or ""
                text = title  # PageIndex stores summary; bench excerpts at search-time
                meta_section: dict[str, Any] = {
                    "grain": "section",
                    "line_num": sh.line_num,
                    "document_id": sh.document_id,
                    "session_date": getattr(sh, "session_date", None),
                }
                out.append(
                    MemoryHit(
                        text=text,
                        score=getattr(sh, "rrf_score", 0.0),
                        fact_type=None,
                        metadata=meta_section,
                        occurred_at=None,
                        source=None,
                        memory_id=f"section:{sh.document_id}:{sh.line_num}",
                        bank_id=request.bank_id,
                        memory_layer="section",
                    )
                )

        # Sort by score desc so the caller's max_results cut picks the
        # top hits across both grains.
        out.sort(key=lambda h: h.score, reverse=True)
        return out


__all__ = ["PageIndexPipeline"]
