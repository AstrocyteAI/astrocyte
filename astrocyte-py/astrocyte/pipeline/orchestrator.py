"""Pipeline orchestrator — coordinates Tier 1 retain/recall/reflect flows.

Async (coordinates I/O stages). See docs/_design/built-in-pipeline.md.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from astrocyte.config import ExtractionProfileConfig, RecallAuthorityConfig

# Re-exported for backward compatibility: these pure module helpers moved to
# _orchestrator_common during the retain/recall/reflect stage extraction but
# remain importable from this module (some tests import them here directly).
from astrocyte.pipeline._orchestrator_common import (  # noqa: E402,F401
    _abstention_floor_for_skepticism,
    _build_cooccurrence_pairs,
    _deterministic_names,
    _entities_from_metadata,
    _resolve_skepticism_for_abstention,
    _source_ids_from_metadata,
    _warn_on_version_drift,
)
from astrocyte.pipeline.agentic_reflect import AgenticReflectParams
from astrocyte.pipeline.chunking import DEFAULT_CHUNK_SIZE
from astrocyte.pipeline.cross_encoder_rerank import (
    CrossEncoderProtocol,
)
from astrocyte.pipeline.fusion import (
    DEFAULT_RRF_K,
)
from astrocyte.pipeline.link_expansion import LinkExpansionParams
from astrocyte.pipeline.recall_stage import RecallStageMixin
from astrocyte.pipeline.reflect_stage import ReflectStageMixin
from astrocyte.pipeline.retain_stage import RetainStageMixin
from astrocyte.policy.signal_quality import DedupDetector
from astrocyte.types import (
    Completion,
    Message,
)

if TYPE_CHECKING:
    from astrocyte.mip.router import MipRouter
    from astrocyte.pipeline.entity_resolution import EntityResolver
    from astrocyte.pipeline.pipeline_config import PipelineConfig
    from astrocyte.provider import DocumentStore, GraphStore, LLMProvider, VectorStore, WikiStore


# Public surface of this module. The _orchestrator_common helpers are
# deliberate re-exports (moved during the stage-mixin extraction; tests and
# older callers import them from here) — listing them in __all__ marks the
# re-export as intentional for linters and CodeQL alike.
__all__ = [  # noqa: RUF022 — grouped: class first, then re-exported helpers
    "PipelineOrchestrator",
    "_abstention_floor_for_skepticism",
    "_build_cooccurrence_pairs",
    "_deterministic_names",
    "_entities_from_metadata",
    "_resolve_skepticism_for_abstention",
    "_source_ids_from_metadata",
    "_warn_on_version_drift",
]

_logger = logging.getLogger("astrocyte.mip")


class _RetainProfiler:
    """Aggregate per-stage timings during retain for evidence-driven
    bottleneck identification.

    Disabled unless ``ASTROCYTE_RETAIN_PROFILE=1`` (env var) — the cost
    of the time.monotonic() calls is small but non-zero and we don't
    want it on by default in production. When enabled, the orchestrator
    captures wall time for each suspected hot path (SFE LLM, embedding
    generation, vector insert, entity merge, entity resolution) and
    emits an aggregated p50/p95/max breakdown via :meth:`report`.

    The samples accumulate across the lifetime of the orchestrator so a
    single bench run produces one breakdown that covers all retain
    calls. Call :meth:`reset` between batches if you want per-batch
    isolation.

    Why not Prometheus / OTel: those exist (``observability.*`` config),
    but require running collectors and a separate analysis stack just
    to find a bottleneck. This is a stop-gap for dev/bench
    investigation — write data once, print at end of run, done.
    """

    def __init__(self) -> None:
        self.enabled = os.environ.get("ASTROCYTE_RETAIN_PROFILE") == "1"
        self.samples: dict[str, list[float]] = defaultdict(list)

    @asynccontextmanager
    async def time(self, stage: str):
        """Async context manager that records elapsed wall time (ms)
        under ``stage`` if profiling is enabled. No-op otherwise so the
        production hot path stays clean."""
        if not self.enabled:
            yield
            return
        t0 = time.monotonic()
        try:
            yield
        finally:
            self.samples[stage].append((time.monotonic() - t0) * 1000.0)

    def reset(self) -> None:
        self.samples.clear()

    def report(self, prefix: str = "[retain.profile]") -> None:
        """Print p50/p95/max for every recorded stage, ordered by total
        time (descending). The dominant stage is first — that's what
        you want to optimize.

        Uses ``print`` rather than the module logger so the breakdown
        always reaches stdout regardless of how the caller configured
        logging — this is dev/bench tooling output, not production
        telemetry, and we want it to be impossible to lose."""
        if not self.enabled:
            print(f"{prefix} (profiler disabled — set ASTROCYTE_RETAIN_PROFILE=1)")
            return
        if not self.samples:
            print(f"{prefix} (profiler enabled but captured no samples — instrumentation unwired?)")
            return
        import statistics

        rows: list[tuple[str, int, float, float, float, float]] = []
        for stage, samples in self.samples.items():
            if not samples:
                continue
            rows.append(
                (
                    stage,
                    len(samples),
                    sum(samples),
                    statistics.median(samples),
                    statistics.quantiles(samples, n=20)[18] if len(samples) >= 20 else max(samples),
                    max(samples),
                ),
            )
        # Sort by total descending — biggest cost first.
        rows.sort(key=lambda r: r[2], reverse=True)
        print(f"{prefix} aggregate breakdown (sorted by total wall time):")
        print(
            f"{prefix}  {'stage':<22} {'n':<7} {'total_ms':<12} {'p50_ms':<10} {'p95_ms':<10} {'max_ms':<10}",
        )
        for stage, n, total, p50, p95, mx in rows:
            print(
                f"{prefix}  {stage:<22} {n:<7d} {total:<12.0f} {p50:<10.1f} {p95:<10.1f} {mx:<10.1f}",
            )


class _TrackingLLMProvider:
    """Transparent wrapper that accumulates token usage from an LLMProvider.

    All calls are forwarded to the underlying provider. After each ``complete()``
    call, ``Completion.usage`` (if present) is added to ``tokens_used``.
    Embedding calls do not consume completion tokens and are forwarded as-is.

    This is used internally by :class:`PipelineOrchestrator` so that the
    evaluation framework can report ``total_tokens_used`` per run without
    modifying individual pipeline modules. See ``evaluation.md`` §2.2.
    """

    SPI_VERSION: int = 1

    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner
        self.tokens_used: int = 0

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        tools: list | None = None,  # list[ToolDefinition] — kept loose to avoid import cycle
        tool_choice: str | None = None,
        response_format: dict | None = None,
    ) -> Completion:
        # Forward native function-calling kwargs through to the underlying
        # provider so the agentic reflect loop (Hindsight parity) can use
        # ``tools=``/``tool_choice=`` end-to-end. Without this, the loop
        # silently fell back to forced single-shot synthesis on every
        # call — observed in 1986/1986 questions on the 2026-05-01 bench.
        #
        # Backward compat: only thread the new kwargs when the caller
        # actually supplied them. Legacy providers / test fakes whose
        # ``complete()`` signatures predate the tools/response_format
        # extensions keep working when invoked via the old text-only
        # path.
        extra_kwargs: dict = {}
        if tools is not None:
            extra_kwargs["tools"] = tools
        if tool_choice is not None:
            extra_kwargs["tool_choice"] = tool_choice
        if response_format is not None:
            extra_kwargs["response_format"] = response_format
        result = await self._inner.complete(
            messages,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            **extra_kwargs,
        )
        if result.usage:
            self.tokens_used += result.usage.input_tokens + result.usage.output_tokens
            # Tier-3 observability: record cost + per-phase tokens when a
            # benchmark collector is attached. Silent no-op otherwise.
            collector = getattr(self, "_metrics_collector", None)
            if collector is not None:
                try:
                    collector.record_completion_call(
                        model=getattr(result, "model", None) or "",
                        input_tokens=result.usage.input_tokens,
                        output_tokens=result.usage.output_tokens,
                    )
                except Exception:
                    pass  # never let metrics break a real call
        return result

    async def embed(
        self,
        texts: list[str],
        model: str | None = None,
    ) -> list[list[float]]:
        result = await self._inner.embed(texts, model=model)
        # Embeddings don't return usage objects; estimate input tokens from
        # the rough heuristic of ~1 token per 4 chars per text (conservative).
        collector = getattr(self, "_metrics_collector", None)
        if collector is not None:
            try:
                est_tokens = sum(max(1, len(t) // 4) for t in texts)
                collector.record_embedding_call(
                    model=model or "text-embedding-3-small",
                    tokens=est_tokens,
                )
            except Exception:
                pass  # metrics are best-effort; never block the embedding call
        return result

    def capabilities(self) -> Any:
        return self._inner.capabilities()

    def reset_tokens(self) -> int:
        """Return accumulated tokens and reset counter to zero."""
        total = self.tokens_used
        self.tokens_used = 0
        return total


class PipelineOrchestrator(RetainStageMixin, RecallStageMixin, ReflectStageMixin):
    """Orchestrates the Tier 1 built-in intelligence pipeline.

    Coordinates async stages: chunk → embed → store → retrieve → fuse → rerank.
    """

    def __init__(
        self,
        vector_store: VectorStore,
        llm_provider: LLMProvider,
        graph_store: GraphStore | None = None,
        document_store: DocumentStore | None = None,
        chunk_strategy: str = "sentence",
        max_chunk_size: int = DEFAULT_CHUNK_SIZE,
        rrf_k: int = DEFAULT_RRF_K,
        semantic_overfetch: int = 5,
        extraction_profiles: dict[str, ExtractionProfileConfig] | None = None,
        *,
        enable_temporal_retrieval: bool = True,
        temporal_scan_cap: int = 500,
        temporal_half_life_days: float = 7.0,
        enable_intent_aware_recall: bool = True,
        enable_multi_query_expansion: bool = True,
        enable_hyde: bool = False,
        wiki_store: WikiStore | None = None,
        wiki_confidence_threshold: float = 0.7,
        entity_resolver: EntityResolver | None = None,
        enable_observation_consolidation: bool = True,
        observation_weight: float = 0.0,
        observation_injection_weight: float = 1.5,
        multi_query_confidence_threshold: float = 0.72,
        final_rerank_mode: str = "heuristic",
        final_rerank_top_n: int = 30,
        final_rerank_keep_n: int | None = None,
    ) -> None:
        self.vector_store = vector_store
        self._tracker = _TrackingLLMProvider(llm_provider)
        self.llm_provider: LLMProvider = self._tracker  # type: ignore[assignment]
        self.graph_store = graph_store
        self.document_store = document_store
        self.chunk_strategy = chunk_strategy
        self.max_chunk_size = max_chunk_size
        self.extraction_profiles = extraction_profiles
        self.rrf_k = rrf_k
        self.semantic_overfetch = semantic_overfetch
        # Temporal retrieval knobs — see astrocyte.pipeline.retrieval for full
        # semantics. Enabled by default: the strategy no-ops gracefully on
        # banks whose vectors have no timestamps, so enabling it is safe.
        self.enable_temporal_retrieval = enable_temporal_retrieval
        self.temporal_scan_cap = temporal_scan_cap
        self.temporal_half_life_days = temporal_half_life_days
        # M9 BM25-IDF keyword strategy. When True (and the document store
        # advertises ``search_fulltext_bm25``), the keyword leg routes
        # through the materialized-view path with corpus IDF + length
        # normalisation instead of the classic ``ts_rank_cd``. Wired by
        # ``Astrocyte.set_pipeline`` from ``bm25_idf.enabled`` config.
        self.bm25_idf_enabled: bool = False
        #: M10 source-aware retain + recall. ``source_store`` is the
        #: SourceStore instance (or ``None``); the three flags below
        #: control behaviour. All wired by ``Astrocyte.set_pipeline`` from
        #: the ``source_aware_retrieval`` config block.
        self.source_store: object | None = None
        self.source_retain_provenance: bool = False
        self.source_chunk_expansion: bool = False
        self.source_expansion_score_multiplier: float = 0.5
        self.source_expansion_max_per_hit: int = 4
        # Intent-aware recall: heuristic query classifier biases RRF
        # weights per strategy. Conservative (always fuses all strategies
        # even under bias), so enabling is safe — a misclassification
        # degrades to a soft lean rather than a strategy drop.
        self.enable_intent_aware_recall = enable_intent_aware_recall
        # Multi-query expansion: decompose complex questions into sub-questions,
        # recall for each independently, and merge via RRF. Improves multi-hop
        # coverage at the cost of N-1 extra embedding + retrieval passes per query.
        # Disabled by default; enable for multi-hop-heavy workloads.
        # When enabled, the confidence gate (multi_query_confidence_threshold)
        # suppresses expansion when the top raw semantic score already exceeds the
        # threshold — avoiding costly sub-query decomposition when a direct answer
        # is already retrieved with high confidence. Cosine similarity is used as
        # the gate signal (not RRF scores, which are rank-based and not comparable
        # across query runs). Default threshold 0.72 passes ~20-30% of queries.
        self.enable_multi_query_expansion = enable_multi_query_expansion
        self.multi_query_confidence_threshold: float = multi_query_confidence_threshold
        # HyDE (R1): generate a hypothetical answer, embed it, and run a second
        # semantic search pass with that vector.  Disabled by default — adds one
        # LLM call per recall.  Enable for multi-hop / paraphrase-heavy workloads.
        self.enable_hyde = enable_hyde
        self._dedup = DedupDetector(similarity_threshold=0.95)
        # Forget-cache invalidation: ``Astrocyte.forget`` calls this hook so
        # the in-memory dedup cache doesn't keep matching against memories
        # that are gone from the vector store. Without this, re-retain after
        # forget silently returns ``stored=False, error="All chunks are
        # near-duplicates"`` for similar content (see invalidate_dedup_cache
        # below).
        #: Per-stage retain timing aggregator. No-op unless
        #: ``ASTROCYTE_RETAIN_PROFILE=1`` is set in the environment;
        #: when enabled, retain_many wraps suspect call sites and the
        #: caller can inspect the breakdown via ``profiler.report()``
        #: or read raw samples from ``profiler.samples``.
        self._profiler = _RetainProfiler()
        #: Set by :meth:`astrocyte._astrocyte.Astrocyte.set_pipeline` when ``recall_authority`` is configured.
        self.recall_authority: RecallAuthorityConfig | None = None
        #: Set by :meth:`astrocyte._astrocyte.Astrocyte.set_pipeline` when
        #: ``cross_encoder_rerank.enabled`` is true. ``None`` falls back to
        #: the heuristic ``cross_encoder_like_rerank`` in ``_rank_reflect_context``.
        self.cross_encoder: CrossEncoderProtocol | None = None
        #: When ``cross_encoder`` is set, only the first ``cross_encoder_top_k``
        #: candidates are scored to bound inference cost. Default mirrors the
        #: config default (30).
        self.cross_encoder_top_k: int = 30
        #: Set by :meth:`astrocyte._astrocyte.Astrocyte.set_pipeline` when
        #: ``link_expansion.enabled`` is true. ``None`` skips the
        #: post-fusion 3-signal expansion. Replaced the old BFS-hop
        #: ``spreading_activation_params`` per Hindsight C3 rewrite.
        self.link_expansion_params: LinkExpansionParams | None = None
        #: Adversarial-defense score-floor abstention. When the top
        #: recall hit's score is below this floor, reflect short-circuits
        #: to "insufficient evidence" without invoking the LLM. Targets
        #: adversarial questions where the LLM left to its own devices
        #: would hallucinate an answer from weak hits. Wired by
        #: ``Astrocyte.set_pipeline`` from ``adversarial_defense`` config.
        self.adversarial_abstention_enabled: bool = False
        self.adversarial_abstention_floor: float = 0.2
        #: Pre-loop premise verification — decompose question into atomic
        #: claims, verify each. Wired below in the reflect path.
        self.adversarial_premise_verification_enabled: bool = False
        self.adversarial_premise_min_confidence: float = 0.6
        #: Tighten the agentic-reflect system prompt with explicit
        #: adversarial-defense rules ("insufficient evidence is always
        #: a valid answer", premise-check, etc.).
        self.adversarial_prompt_enabled: bool = False
        #: Hindsight-parity causal-link extraction at retain time. When
        #: enabled, one extra LLM call per record produces directional
        #: ``causes`` edges. Wired by ``Astrocyte.set_pipeline``.
        self.causal_links_enabled: bool = False
        self.causal_max_pairs_per_memory: int = 4
        self.causal_min_confidence: float = 0.7
        #: Hindsight-parity semantic-kNN graph (C3a). When enabled, each
        #: new memory at retain time gets ``MemoryLink(link_type="semantic")``
        #: edges to its top-K most-similar existing memories above the
        #: similarity threshold. Wired by ``Astrocyte.set_pipeline``.
        self.semantic_link_graph_enabled: bool = False
        self.semantic_link_graph_top_k: int = 5
        self.semantic_link_graph_threshold: float = 0.7
        #: Structured fact extraction at retain time (5-dim
        #: what/when/where/who/why with embedded entities + caused_by
        #: relations). When enabled, replaces chunk_text +
        #: extract_entities + fact_causal_extraction with a single
        #: LLM call producing structured facts. Each fact becomes one
        #: memory.
        self.structured_fact_extraction_enabled: bool = False
        self.structured_fact_extraction_max_facts: int = 30
        #: Mode: "verbatim" stores raw chunk text + metadata (preserves
        #: vocabulary for embedding-match), "concise" stores LLM-paraphrased
        #: structured facts. Default verbatim because of the recall_hit_rate
        #: regression that concise paraphrasing causes.
        self.structured_fact_extraction_mode: str = "verbatim"
        #: Chunking strategy used by verbatim SFE pre-chunking. Defaults
        #: to "paragraph" (which gives the LLM full session context for
        #: metadata extraction); LoCoMo benchmark losses 2.5 pts overall
        #: when set to "dialogue".
        self.structured_fact_extraction_chunk_strategy: str = "paragraph"
        #: Per-chunk character budget for verbatim SFE pre-chunking.
        #: When ``None`` the SFE path falls through to the same
        #: chunk-size resolver legacy retain uses (orchestrator default
        #: 512 chars). Bumping this is the dominant retain throughput
        #: lever for SFE — fewer chunks = fewer ``facts[]`` entries the
        #: LLM has to emit per session, and gpt-4o-mini latency is
        #: roughly linear in output tokens. 2048 measured ~4× on LME-
        #: shaped traffic without accuracy regression on LoCoMo.
        self.structured_fact_extraction_chunk_max_size: int | None = None
        #: Per-chunk parallel verbatim extraction (Phase 3 of cost-
        #: control port). When True, the SFE path sends one LLM call
        #: per chunk in parallel rather than one batched call. Drops
        #: cross-chunk causal_relations — pair with
        #: ``causal_links.enabled=false``.
        self.structured_fact_extraction_parallel_chunks: bool = False
        #: Max in-flight LLM calls per session when
        #: ``parallel_chunks`` is True.
        self.structured_fact_extraction_parallel_chunks_max_concurrency: int = 6
        #: Entity co-occurrence link cap (2026-05-06 retain-profile fix).
        #: When ``enabled``, retain creates ``co_occurs`` links between
        #: at most ``max_entities`` entities per memory — bounding the
        #: Cartesian product to ``C(K,2)`` per retain regardless of N.
        #: Profiling on LME measured the unbounded path at 34% of
        #: retain wall with O(N²) drift; capping at K=5 brings it to
        #: <1% steady state.
        self.entity_cooccurrence_enabled: bool = True
        self.entity_cooccurrence_max_entities: int = 5
        #: Query-level temporal constraint extraction. When enabled,
        #: recall parses temporal expressions in the query into a
        #: time_range filter applied to retrieval. Regex pre-pass is
        #: free; ``allow_llm_fallback`` opt-in adds 1 LLM call per
        #: temporal-marker query.
        self.query_analyzer_enabled: bool = False
        self.query_analyzer_allow_llm_fallback: bool = True
        #: M18a-1 — extended temporal-expansion pattern set in the regex
        #: pre-pass (word-numbers, "a few X ago", "the other day", "this/
        #: earlier this <unit>", "recently/lately"). Default False.
        #: Flipped via per-bank config or env override
        #: ``ASTROCYTE_M18_ENABLE_TEMPORAL_EXPANSION=1`` for bench runs.
        self.query_analyzer_enable_temporal_expansion: bool = False
        #: Hindsight-parity agentic reflect loop. ``None`` = single-shot
        #: synthesis (legacy path). Set by ``Astrocyte.set_pipeline``
        #: when ``agentic_reflect.enabled`` is true.
        self.agentic_reflect_params: AgenticReflectParams | None = None
        #: Set by :meth:`astrocyte._astrocyte.Astrocyte.set_pipeline` when MIP is configured.
        #: Used by :meth:`recall` to resolve per-bank rerank/reflect overrides (P3).
        self.mip_router: MipRouter | None = None
        # M8 W5 — wiki tier precedence.  When a WikiStore is wired up and a
        # compiled wiki page scores above ``wiki_confidence_threshold``, recall
        # returns the wiki hit + raw-memory citations instead of running the full
        # parallel-retrieve / RRF pipeline.
        self.wiki_store: WikiStore | None = wiki_store
        self.wiki_confidence_threshold: float = wiki_confidence_threshold
        # M11: entity resolution — alias-of links between entities.
        # None means the stage is skipped (opt-in, no cost when disabled).
        self.entity_resolver: EntityResolver | None = entity_resolver
        # Observation consolidation — post-retain background LLM pass that
        # maintains a deduplicated observations layer.  Disabled by default
        # (opt-in).  When enabled, recall also runs a separate "observation"
        # strategy that searches the observations layer and fuses results into
        # the main RRF pipeline with a configurable weight boost.
        self.enable_observation_consolidation: bool = enable_observation_consolidation
        self.observation_weight: float = observation_weight
        # Weight applied to the ::obs bank when intent-gated injection fires
        # (EXPLORATORY / RELATIONAL queries). Kept separate from observation_weight
        # so callers can disable global injection (observation_weight=0.0) while
        # still enabling the intent-gated path.
        self.observation_injection_weight: float = observation_injection_weight
        self.final_rerank_mode = final_rerank_mode
        self.final_rerank_top_n = final_rerank_top_n
        self.final_rerank_keep_n = final_rerank_keep_n
        if enable_observation_consolidation:
            from astrocyte.pipeline.observation import ObservationConsolidator

            self._observation_consolidator: ObservationConsolidator | None = ObservationConsolidator()
        else:
            self._observation_consolidator = None
        self._background_tasks: set[asyncio.Task[None]] = set()

        # Mental-model service — wires the agentic reflect loop to the
        # configured ``MentalModelStore`` (typically ``PostgresMentalModelStore``).
        # ``None`` when no store is configured; ``set_mental_model_service``
        # is called from ``Astrocyte.set_pipeline`` when a store is present.
        # When set, the agent gets ``search_mental_models`` as a tool —
        # the highest-quality tier in the hierarchical priority order
        # (mental_models → observations → recall → expand → done).
        self.mental_model_service: object | None = None

    def apply_config(self, cfg: PipelineConfig) -> None:
        """Apply a resolved :class:`PipelineConfig` to this orchestrator.

        Replaces the old inline attribute-poking in ``Astrocyte.set_pipeline``:
        the config is derived once (see :meth:`PipelineConfig.from_config`) and
        applied here in a single place. Field names on ``PipelineConfig`` mirror
        the orchestrator attributes exactly, so this is a flat assignment — any
        drift between the two surfaces is a loud ``AttributeError`` at wiring
        time rather than a silently-ignored flag.
        """
        for name, value in cfg.as_orchestrator_attrs().items():
            if not hasattr(self, name):
                raise AttributeError(
                    f"PipelineConfig field {name!r} has no matching orchestrator attribute"
                )
            setattr(self, name, value)

    @property
    def tokens_used(self) -> int:
        """Total LLM tokens consumed through this orchestrator since last reset."""
        return self._tracker.tokens_used

    def reset_token_counter(self) -> int:
        """Return accumulated token count and reset to zero."""
        return self._tracker.reset_tokens()

    def invalidate_dedup_cache(
        self,
        bank_id: str,
        memory_ids: list[str] | None = None,
    ) -> None:
        """Drop forgotten memories from the in-memory dedup cache.

        Called by ``Astrocyte.forget`` after the underlying store has
        accepted the forget. Without this, a re-retain of similar content
        after forget hits the in-memory ``DedupDetector`` cache (still
        holding the forgotten memory's embedding) and the pipeline
        short-circuits with ``RetainResult(stored=False, deduplicated=True,
        error="All chunks are near-duplicates")`` — a silent no-op from
        the caller's perspective.

        Args:
            bank_id: The bank the forget targeted.
            memory_ids: Specific memories to drop from the cache. ``None``
                (the whole-bank or tag-filtered forget path) clears the
                entire bank's cache — coarser but always-correct.
        """
        if memory_ids is None:
            self._dedup.clear_bank(bank_id)
            return

        for mid in memory_ids:
            self._dedup.remove(bank_id, mid)


    async def shutdown(self) -> None:
        """Drain background work and close provider resources owned by the pipeline."""
        if self._background_tasks:
            _, pending = await asyncio.wait(self._background_tasks, timeout=2.0)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        for provider in (self.vector_store, self.graph_store, self.document_store):
            close = getattr(provider, "close", None)
            if close is None:
                continue
            try:
                result = close()
                if inspect.isawaitable(result):
                    _ = await result
            except Exception as exc:
                _logger.warning("provider close failed during pipeline shutdown: %s", exc)
