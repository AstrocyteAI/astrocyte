"""Multi-bank recall orchestrator — parallel, cascade, and first-match strategies."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from astrocyte.errors import ConfigError
from astrocyte.policy.homeostasis import enforce_token_budget
from astrocyte.policy.observability import MetricsCollector
from astrocyte.types import (
    MemoryHit,
    MultiBankStrategy,
    RecallRequest,
    RecallResult,
)

logger = logging.getLogger("astrocyte")

# Type aliases for the callbacks injected from Astrocyte
RecallFn = Callable[[RecallRequest], Awaitable[RecallResult]]
MakeRequestFn = Callable[
    [str, str, int, int | None, list[str] | None, dict[str, Any]],
    Awaitable[RecallRequest],
]


def _bank_visit_order(bank_ids: list[str], cascade_order: list[str] | None) -> list[str]:
    if not cascade_order:
        return list(bank_ids)
    out: list[str] = []
    seen: set[str] = set()
    for b in cascade_order:
        if b in bank_ids and b not in seen:
            out.append(b)
            seen.add(b)
    for b in bank_ids:
        if b not in seen:
            out.append(b)
            seen.add(b)
    return out


def _dedupe_hits_by_text(hits: list[MemoryHit]) -> list[MemoryHit]:
    """One hit per distinct text, keeping the highest-scoring instance."""
    best: dict[str, MemoryHit] = {}
    for h in hits:
        prev = best.get(h.text)
        if prev is None or h.score > prev.score:
            best[h.text] = h
    return sorted(best.values(), key=lambda x: x.score, reverse=True)


def _apply_bank_weights(hits: list[MemoryHit], weights: dict[str, float] | None) -> list[MemoryHit]:
    if not weights:
        return list(hits)
    out: list[MemoryHit] = []
    for h in hits:
        bid = h.bank_id or ""
        w = float(weights.get(bid, 1.0))
        out.append(replace(h, score=h.score * w))
    return out


def _tag_hits_with_bank(hits: list[MemoryHit], bank_id: str) -> list[MemoryHit]:
    return [replace(h, bank_id=bank_id) if h.bank_id is None else h for h in hits]


class MultiBankOrchestrator:
    """Dispatches multi-bank recall across parallel, cascade, and first-match strategies."""

    def __init__(
        self,
        *,
        do_recall: RecallFn,
        make_request: MakeRequestFn,
        circuit_breaker_record_failure: Callable[[], None],
        metrics: MetricsCollector,
        provider_name: str,
    ) -> None:
        self._do_recall = do_recall
        self._make_request = make_request
        self._cb_record_failure = circuit_breaker_record_failure
        self._metrics = metrics
        self._provider_name = provider_name

    async def recall(
        self,
        query: str,
        bank_ids: list[str],
        max_results: int,
        max_tokens: int | None,
        tags: list[str] | None,
        kwargs: dict[str, Any],
        strategy: MultiBankStrategy,
    ) -> RecallResult:
        """Multi-bank recall — strategy dispatch."""
        if strategy.mode == "parallel":
            return await self._parallel(query, bank_ids, max_results, max_tokens, tags, kwargs, strategy)
        if strategy.mode == "cascade":
            return await self._cascade(query, bank_ids, max_results, max_tokens, tags, kwargs, strategy)
        if strategy.mode == "first_match":
            return await self._first_match(query, bank_ids, max_results, max_tokens, tags, kwargs, strategy)
        raise ConfigError(f"Unknown multi-bank mode: {strategy.mode!r}")

    async def _parallel(
        self,
        query: str,
        bank_ids: list[str],
        max_results: int,
        max_tokens: int | None,
        tags: list[str] | None,
        kwargs: dict[str, Any],
        strategy: MultiBankStrategy,
    ) -> RecallResult:
        reqs: list[RecallRequest] = []
        for bid in bank_ids:
            reqs.append(await self._make_request(query, bid, max_results, None, tags, kwargs))
        tasks = [self._do_recall(r) for r in reqs]

        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            logger.error("Multi-bank parallel recall timed out after 30s for banks: %s", bank_ids)
            self._metrics.inc_counter(
                "astrocyte_multi_bank_recall_timeout_total",
                {"bank_ids": ",".join(bank_ids)},
            )
            return RecallResult(hits=[], total_available=0, truncated=False)

        all_hits: list[MemoryHit] = []
        total_available = 0
        for bid, result in zip(bank_ids, results):
            if isinstance(result, RecallResult):
                all_hits.extend(_tag_hits_with_bank(result.hits, bid))
                total_available += result.total_available
            elif isinstance(result, BaseException):
                logger.warning("Multi-bank recall failed for bank '%s': %s", bid, result)
                self._cb_record_failure()
                self._metrics.inc_counter(
                    "astrocyte_recall_total",
                    {"bank_id": bid, "provider": self._provider_name, "status": "error"},
                )

        weighted = _apply_bank_weights(all_hits, strategy.bank_weights)
        weighted.sort(key=lambda h: h.score, reverse=True)

        if strategy.dedup_across_banks:
            deduped = _dedupe_hits_by_text(weighted)
        else:
            deduped = weighted

        trimmed = deduped[:max_results]
        truncated = False
        if max_tokens:
            trimmed, truncated = enforce_token_budget(trimmed, max_tokens)

        return RecallResult(hits=trimmed, total_available=total_available, truncated=truncated)

    async def _cascade(
        self,
        query: str,
        bank_ids: list[str],
        max_results: int,
        max_tokens: int | None,
        tags: list[str] | None,
        kwargs: dict[str, Any],
        strategy: MultiBankStrategy,
    ) -> RecallResult:
        order = _bank_visit_order(bank_ids, strategy.cascade_order)
        accumulated: list[MemoryHit] = []
        total_available = 0

        for bid in order:
            result = await self._do_recall(
                await self._make_request(query, bid, max_results, None, tags, kwargs),
            )
            total_available += result.total_available
            accumulated.extend(_tag_hits_with_bank(result.hits, bid))

            merged_for_stop = _dedupe_hits_by_text(accumulated) if strategy.dedup_across_banks else list(accumulated)
            if len(merged_for_stop) >= strategy.min_results_to_stop:
                break

        working = _dedupe_hits_by_text(accumulated) if strategy.dedup_across_banks else accumulated
        weighted = _apply_bank_weights(working, strategy.bank_weights)
        weighted.sort(key=lambda h: h.score, reverse=True)
        trimmed = weighted[:max_results]
        truncated = False
        if max_tokens:
            trimmed, truncated = enforce_token_budget(trimmed, max_tokens)
        return RecallResult(hits=trimmed, total_available=total_available, truncated=truncated)

    async def _first_match(
        self,
        query: str,
        bank_ids: list[str],
        max_results: int,
        max_tokens: int | None,
        tags: list[str] | None,
        kwargs: dict[str, Any],
        strategy: MultiBankStrategy,
    ) -> RecallResult:
        order = _bank_visit_order(bank_ids, strategy.cascade_order)
        total_available = 0
        for bid in order:
            result = await self._do_recall(
                await self._make_request(query, bid, max_results, None, tags, kwargs),
            )
            total_available += result.total_available
            if result.hits:
                hits = _tag_hits_with_bank(result.hits, bid)
                hits = hits[:max_results]
                hits = _apply_bank_weights(hits, strategy.bank_weights)
                hits.sort(key=lambda h: h.score, reverse=True)
                truncated = False
                if max_tokens:
                    hits, truncated = enforce_token_budget(hits, max_tokens)
                return RecallResult(hits=hits, total_available=total_available, truncated=truncated)
        return RecallResult(hits=[], total_available=total_available, truncated=False)
