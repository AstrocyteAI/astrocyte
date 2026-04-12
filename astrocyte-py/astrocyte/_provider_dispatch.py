"""Provider dispatcher — routes operations to engine provider or pipeline."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from astrocyte.errors import CapabilityNotSupported, ConfigError
from astrocyte.recall.merge_result import merge_external_into_recall_result
from astrocyte.types import (
    ForgetRequest,
    ForgetResult,
    MemoryHit,
    RecallRequest,
    RecallResult,
    ReflectRequest,
    ReflectResult,
    RetainRequest,
    RetainResult,
)

if TYPE_CHECKING:
    from astrocyte.config import AstrocyteConfig
    from astrocyte.pipeline.orchestrator import PipelineOrchestrator
    from astrocyte.pipeline.tiered_retrieval import TieredRetriever
    from astrocyte.provider import EngineProvider
    from astrocyte.types import EngineCapabilities

logger = logging.getLogger("astrocyte")


class ProviderDispatcher:
    """Routes retain/recall/reflect/forget to the configured engine or pipeline."""

    def __init__(
        self,
        config: AstrocyteConfig,
        engine_provider: EngineProvider | None = None,
        pipeline: PipelineOrchestrator | None = None,
        capabilities: EngineCapabilities | None = None,
        tiered_retriever: TieredRetriever | None = None,
    ) -> None:
        self._config = config
        self.engine_provider = engine_provider
        self.pipeline = pipeline
        self.capabilities = capabilities
        self.tiered_retriever = tiered_retriever

    @property
    def provider_name(self) -> str:
        return self._config.provider or "pipeline"

    async def retain(self, request: RetainRequest) -> RetainResult:
        if self.engine_provider:
            return await self.engine_provider.retain(request)
        if self.pipeline:
            return await self.pipeline.retain(request)
        raise ConfigError("No provider or pipeline configured")

    async def recall(self, request: RecallRequest) -> RecallResult:
        if self.engine_provider:
            if self.tiered_retriever is not None and self._config.tiered_retrieval.full_recall == "hybrid":
                return await self.tiered_retriever.retrieve(request)
            result = await self.engine_provider.recall(request)
            # Hybrid merges pipeline (which already fuses external_context in RRF); do not merge twice.
            from astrocyte.hybrid import HybridEngineProvider

            if request.external_context and not isinstance(self.engine_provider, HybridEngineProvider):
                return merge_external_into_recall_result(result, request.external_context, request.max_results)
            return result
        if self.pipeline:
            if self.tiered_retriever is not None:
                return await self.tiered_retriever.retrieve(request)
            return await self.pipeline.recall(request)
        raise ConfigError("No provider or pipeline configured")

    async def reflect(self, request: ReflectRequest) -> ReflectResult:
        # Check if provider supports reflect
        if self.engine_provider:
            if self.capabilities and self.capabilities.supports_reflect:
                return await self.engine_provider.reflect(request)
            # Fallback
            if self._config.fallback_strategy == "error":
                raise CapabilityNotSupported(self.provider_name, "reflect")
            if self._config.fallback_strategy == "degrade":
                # Return recall results as-is
                recall_result = await self.recall(
                    RecallRequest(query=request.query, bank_id=request.bank_id, max_results=10)
                )
                return ReflectResult(
                    answer="\n".join(h.text for h in recall_result.hits),
                    sources=recall_result.hits,
                )
            # local_llm fallback needs pipeline's reflect
            if self.pipeline:
                return await self.pipeline.reflect(request)
            raise CapabilityNotSupported(self.provider_name, "reflect")

        if self.pipeline:
            return await self.pipeline.reflect(request)

        raise ConfigError("No provider or pipeline configured")

    async def forget(self, request: ForgetRequest) -> ForgetResult:
        if self.engine_provider:
            if self.capabilities and self.capabilities.supports_forget:
                return await self.engine_provider.forget(request)
            raise CapabilityNotSupported(self.provider_name, "forget")
        # Pipeline: delete from vector store
        if self.pipeline:
            if request.scope == "all" and hasattr(self.pipeline.vector_store, "list_vectors"):
                # Delete all vectors in bank by paginating through them
                total_deleted = 0
                while True:
                    batch = await self.pipeline.vector_store.list_vectors(request.bank_id, offset=0, limit=100)
                    if not batch:
                        break
                    ids = [v.id for v in batch]
                    total_deleted += await self.pipeline.vector_store.delete(ids, request.bank_id)
                return ForgetResult(deleted_count=total_deleted)
            if request.memory_ids:
                count = await self.pipeline.vector_store.delete(request.memory_ids, request.bank_id)
                return ForgetResult(deleted_count=count)
        raise CapabilityNotSupported(self.provider_name, "forget")

    async def reflect_from_hits(
        self,
        query: str,
        hits: list[MemoryHit],
        bank_id: str,
        max_tokens: int | None = None,
        dispositions: Any = None,
        authority_context: str | None = None,
    ) -> ReflectResult:
        """Synthesize over pre-fetched hits (used by multi-bank reflect).

        Tries in order:
        1. Pipeline reflect (if available) — calls synthesize() directly.
        2. Degrade fallback — concatenate hit texts.
        3. Empty answer if no hits.
        """
        # If we have a pipeline with an LLM, use its synthesis directly
        if self.pipeline:
            from astrocyte.pipeline.reflect import synthesize

            return await synthesize(
                query=query,
                hits=hits,
                llm_provider=self.pipeline.llm_provider,
                dispositions=dispositions,
                max_tokens=max_tokens or 2048,
                authority_context=authority_context,
            )

        # Fall back to degrade mode: concatenate hit texts as the answer.
        if hits:
            return ReflectResult(
                answer="\n".join(h.text for h in hits),
                sources=hits,
            )

        return ReflectResult(answer="No relevant memories found across banks.", sources=[])
