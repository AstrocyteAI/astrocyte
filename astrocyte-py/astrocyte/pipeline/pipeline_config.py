"""Feature-flag configuration for :class:`PipelineOrchestrator`.

Historically the orchestrator was built with base dependencies and *then*
``Astrocyte.set_pipeline`` reached in and mutated ~30 feature-flag attributes
one by one from ``AstrocyteConfig`` blocks — a two-phase construction where a
foreign class finished wiring the orchestrator by poking its internals.

``PipelineConfig`` collapses that translation into one place. Build it once
with :meth:`PipelineConfig.from_config` and hand it to
:meth:`PipelineOrchestrator.apply_config`; the orchestrator no longer has its
configuration assembled by an outside method. The flag *storage* still lives on
the orchestrator (so the 3k-line hot paths read ``self.<flag>`` unchanged and
direct-construction tests keep their constructor defaults), but the *derivation*
of those flags from config now has a single, testable owner.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrocyte.config import AstrocyteConfig, ExtractionProfileConfig, RecallAuthorityConfig
    from astrocyte.pipeline.agentic_reflect import AgenticReflectParams
    from astrocyte.pipeline.cross_encoder_rerank import CrossEncoderProtocol
    from astrocyte.pipeline.link_expansion import LinkExpansionParams


def _temporal_expansion_flag(config_default: bool) -> bool:
    """Resolve query-analyzer temporal expansion, honouring the env override.

    ``ASTROCYTE_M18_ENABLE_TEMPORAL_EXPANSION`` gives bench-time control without
    a code change; otherwise the per-bank config value wins.
    """
    env = os.environ.get("ASTROCYTE_M18_ENABLE_TEMPORAL_EXPANSION", "").lower()
    if env in ("1", "true", "yes"):
        return True
    if env in ("0", "false", "no"):
        return False
    return config_default


@dataclass(frozen=True)
class PipelineConfig:
    """Everything ``set_pipeline`` used to mutate onto the orchestrator, resolved once.

    Field names mirror the orchestrator attributes they populate, so
    :meth:`PipelineOrchestrator.apply_config` is a flat, obviously-correct
    assignment with no per-field translation.
    """

    # Extraction / authority.
    extraction_profiles: dict[str, ExtractionProfileConfig] | None
    recall_authority: RecallAuthorityConfig | None

    # Cross-encoder rerank (Hindsight parity). ``cross_encoder`` is a loaded
    # model handle or ``None`` (heuristic fallback).
    cross_encoder: CrossEncoderProtocol | None
    cross_encoder_top_k: int

    # Causal-link extraction at retain time.
    causal_links_enabled: bool
    causal_max_pairs_per_memory: int
    causal_min_confidence: float

    # Semantic-kNN graph at retain time (C3a).
    semantic_link_graph_enabled: bool
    semantic_link_graph_top_k: int
    semantic_link_graph_threshold: float

    # Structured fact extraction at retain time.
    structured_fact_extraction_enabled: bool
    structured_fact_extraction_max_facts: int
    structured_fact_extraction_mode: str
    structured_fact_extraction_chunk_strategy: str
    structured_fact_extraction_chunk_max_size: int | None
    structured_fact_extraction_parallel_chunks: bool
    structured_fact_extraction_parallel_chunks_max_concurrency: int

    # Entity co-occurrence link cap.
    entity_cooccurrence_enabled: bool
    entity_cooccurrence_max_entities: int

    # Query analyzer (temporal constraint extraction at recall time).
    query_analyzer_enabled: bool
    query_analyzer_allow_llm_fallback: bool
    query_analyzer_enable_temporal_expansion: bool

    # Link expansion (C3) — ``None`` skips the post-fusion 3-signal expansion.
    link_expansion_params: LinkExpansionParams | None

    # M9 BM25-IDF keyword strategy.
    bm25_idf_enabled: bool

    # M10 source-aware retain + recall.
    source_store: object | None
    source_retain_provenance: bool
    source_chunk_expansion: bool
    source_expansion_score_multiplier: float
    source_expansion_max_per_hit: int

    # Adversarial defense.
    adversarial_abstention_enabled: bool
    adversarial_abstention_floor: float
    adversarial_premise_verification_enabled: bool
    adversarial_premise_min_confidence: float
    adversarial_prompt_enabled: bool

    # Agentic reflect loop — ``None`` = legacy single-shot synthesis.
    agentic_reflect_params: AgenticReflectParams | None

    # Mental-model service — wires agentic reflect to the configured store.
    mental_model_service: object | None

    @classmethod
    def from_config(
        cls,
        config: AstrocyteConfig,
        *,
        source_store: object | None = None,
        mental_model_store: object | None = None,
    ) -> PipelineConfig:
        """Derive the full flag set from an :class:`AstrocyteConfig`.

        Lazy service construction (cross-encoder model load, agentic-reflect and
        link-expansion params, mental-model service) happens here exactly as it
        did in the old ``set_pipeline`` body — only when the corresponding block
        is enabled, so disabled features stay zero-cost.
        """
        from astrocyte.pipeline.extraction import merged_extraction_profiles

        # Cross-encoder reranker — lazy model load, opt-in.
        cer_cfg = config.cross_encoder_rerank
        cross_encoder: CrossEncoderProtocol | None = None
        cross_encoder_top_k = 30
        if cer_cfg.enabled:
            from astrocyte.pipeline.cross_encoder_rerank import get_default_cross_encoder

            cross_encoder = get_default_cross_encoder(
                cer_cfg.model_name,
                force_cpu=cer_cfg.force_cpu,
            )
            cross_encoder_top_k = cer_cfg.top_k

        # Link expansion (reuses the legacy ``spreading_activation:`` block).
        sa_cfg = config.spreading_activation
        link_expansion_params: LinkExpansionParams | None = None
        if sa_cfg.enabled:
            from astrocyte.pipeline.link_expansion import LinkExpansionParams

            link_expansion_params = LinkExpansionParams(
                expansion_limit=sa_cfg.expansion_limit,
            )

        # Agentic reflect loop.
        ad_cfg = config.adversarial_defense
        ar_cfg = config.agentic_reflect
        agentic_reflect_params: AgenticReflectParams | None = None
        if ar_cfg.enabled:
            from astrocyte.pipeline.agentic_reflect import AgenticReflectParams

            agentic_reflect_params = AgenticReflectParams(
                max_iterations=ar_cfg.max_iterations,
                recall_step_max_results=ar_cfg.recall_step_max_results,
                max_evidence_pool_size=ar_cfg.max_evidence_pool_size,
                # Mirrors the separate adversarial_defense block so enabling
                # adversarial defense also tightens the loop's system prompt.
                adversarial_defense=ad_cfg.adversarial_prompt_enabled,
            )

        # Mental-model service — present only when a store is configured.
        mental_model_service: object | None = None
        if mental_model_store is not None:
            from astrocyte.pipeline.mental_model import MentalModelService

            mental_model_service = MentalModelService(mental_model_store)  # type: ignore[arg-type]

        cl_cfg = config.causal_links
        slg_cfg = config.semantic_link_graph
        sfe_cfg = config.structured_fact_extraction
        coocc_cfg = config.entity_cooccurrence
        qa_cfg = config.query_analyzer
        sar_cfg = config.source_aware_retrieval

        return cls(
            extraction_profiles=merged_extraction_profiles(config),
            recall_authority=config.recall_authority,
            cross_encoder=cross_encoder,
            cross_encoder_top_k=cross_encoder_top_k,
            causal_links_enabled=cl_cfg.enabled,
            causal_max_pairs_per_memory=cl_cfg.max_pairs_per_memory,
            causal_min_confidence=cl_cfg.min_confidence,
            semantic_link_graph_enabled=slg_cfg.enabled,
            semantic_link_graph_top_k=slg_cfg.top_k,
            semantic_link_graph_threshold=slg_cfg.similarity_threshold,
            structured_fact_extraction_enabled=sfe_cfg.enabled,
            structured_fact_extraction_max_facts=sfe_cfg.max_facts_per_call,
            structured_fact_extraction_mode=sfe_cfg.extraction_mode,
            structured_fact_extraction_chunk_strategy=sfe_cfg.chunk_strategy,
            structured_fact_extraction_chunk_max_size=sfe_cfg.chunk_max_size,
            structured_fact_extraction_parallel_chunks=sfe_cfg.parallel_chunks,
            structured_fact_extraction_parallel_chunks_max_concurrency=sfe_cfg.parallel_chunks_max_concurrency,
            entity_cooccurrence_enabled=coocc_cfg.enabled,
            entity_cooccurrence_max_entities=coocc_cfg.max_entities_per_memory,
            query_analyzer_enabled=qa_cfg.enabled,
            query_analyzer_allow_llm_fallback=qa_cfg.allow_llm_fallback,
            query_analyzer_enable_temporal_expansion=_temporal_expansion_flag(
                qa_cfg.enable_temporal_expansion
            ),
            link_expansion_params=link_expansion_params,
            bm25_idf_enabled=config.bm25_idf.enabled,
            source_store=source_store,
            source_retain_provenance=sar_cfg.retain_provenance,
            source_chunk_expansion=sar_cfg.chunk_expansion,
            source_expansion_score_multiplier=sar_cfg.expansion_score_multiplier,
            source_expansion_max_per_hit=sar_cfg.expansion_max_per_hit,
            adversarial_abstention_enabled=ad_cfg.abstention_enabled,
            adversarial_abstention_floor=ad_cfg.abstention_floor,
            adversarial_premise_verification_enabled=ad_cfg.premise_verification_enabled,
            adversarial_premise_min_confidence=ad_cfg.premise_verification_min_confidence,
            adversarial_prompt_enabled=ad_cfg.adversarial_prompt_enabled,
            agentic_reflect_params=agentic_reflect_params,
            mental_model_service=mental_model_service,
        )

    def as_orchestrator_attrs(self) -> dict[str, Any]:
        """The flag set as a flat ``{attr_name: value}`` map for assignment."""
        return dict(self.__dict__)
