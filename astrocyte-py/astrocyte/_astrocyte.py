"""Core Astrocyte class — the main entry point for the framework.

Handles tier routing, policy enforcement, capability negotiation,
multi-bank orchestration, and hook dispatch.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from astrocyte._hooks import HookHandler, HookManager
from astrocyte._multi_bank import MultiBankOrchestrator
from astrocyte._output_scanner import OutputScanner
from astrocyte._policy import PolicyEnforcer
from astrocyte._provider_dispatch import ProviderDispatcher
from astrocyte._recall_params import RecallParams
from astrocyte._validation import validate_bank_id
from astrocyte.analytics import BankMetricsCollector, compute_bank_health, counters_to_quality_point
from astrocyte.config import AstrocyteConfig, load_config
from astrocyte.errors import (
    AccessDenied,
    ConfigError,
    MipRoutingError,
    ProviderUnavailable,
)
from astrocyte.identity import context_principal_label
from astrocyte.lifecycle import LifecycleManager
from astrocyte.mip.router import MipRouter
from astrocyte.policy.observability import MetricsCollector, StructuredLogger, span, timed
from astrocyte.recall.authority import apply_recall_authority
from astrocyte.types import (
    AccessGrant,
    AstrocyteContext,
    AuditResult,
    BankHealth,
    CompileResult,
    ForgetRequest,
    ForgetResult,
    HealthStatus,
    HistoryResult,
    LegalHold,
    LifecycleRunResult,
    MemoryHit,
    MultiBankStrategy,
    QualityDataPoint,
    RecallRequest,
    RecallResult,
    ReflectRequest,
    ReflectResult,
    RetainRequest,
    RetainResult,
)

if TYPE_CHECKING:
    from astrocyte.pipeline.orchestrator import PipelineOrchestrator
    from astrocyte.provider import EngineProvider

logger = logging.getLogger("astrocyte")


def _normalize_multi_bank_strategy(
    strategy: Literal["cascade", "parallel", "first_match"] | MultiBankStrategy | None,
) -> MultiBankStrategy:
    if strategy is None:
        return MultiBankStrategy(mode="parallel")
    if isinstance(strategy, MultiBankStrategy):
        return strategy
    if strategy in ("cascade", "parallel", "first_match"):
        return MultiBankStrategy(mode=strategy)
    raise ConfigError(f"Unknown multi-bank strategy: {strategy!r}")



class Astrocyte:
    """The Astrocyte memory framework — unified API for AI agent memory.

    Usage:
        brain = Astrocyte.from_config("astrocyte.yaml")
        await brain.retain("Calvin prefers dark mode", bank_id="user-123")
        hits = await brain.recall("What are Calvin's preferences?", bank_id="user-123")
    """

    def __init__(self, config: AstrocyteConfig) -> None:
        self._config = config
        self._logger = StructuredLogger(level=config.observability.log_level)

        # Extracted subsystems
        self._policy = PolicyEnforcer(config)
        self._hook_manager = HookManager(self._logger)
        self._output_scanner = OutputScanner(config, self._logger)
        self._dispatcher = ProviderDispatcher(config)

        # Metrics
        self._metrics = MetricsCollector(enabled=config.observability.prometheus_enabled)

        # Lifecycle
        self._lifecycle = LifecycleManager(config.lifecycle)

        # MIP router (loaded from mip.yaml if configured)
        self._mip_router: MipRouter | None = None
        if config.mip_config_path:
            from astrocyte.mip.loader import load_mip_config

            mip_config = load_mip_config(config.mip_config_path)
            self._mip_router = MipRouter(mip_config)

        # Analytics
        self._analytics = BankMetricsCollector()

        # Provider state (managed by dispatcher, exposed for wiring)
        self._engine_provider: EngineProvider | None = None
        self._pipeline: PipelineOrchestrator | None = None

        # Multi-bank orchestrator (wired lazily after provider is set)
        self._multi_bank: MultiBankOrchestrator | None = None

        # M8: wiki store (optional; enables brain.compile())
        self._wiki_store: object | None = None
        # M8 W4: async compile queue (optional; enables automatic threshold triggering)
        self._compile_queue: object | None = None

    @property
    def config(self) -> AstrocyteConfig:
        """Loaded :class:`~astrocyte.config.AstrocyteConfig` (read-only for callers)."""
        return self._config

    async def __aenter__(self) -> "Astrocyte":
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass  # Future: close provider connections

    @classmethod
    def from_config(cls, path: str | Path) -> "Astrocyte":
        """Create an Astrocyte instance from a YAML config file."""
        config = load_config(path)
        return cls(config)

    @classmethod
    def from_config_dict(cls, data: dict[str, str | int | float | bool | None | dict | list]) -> "Astrocyte":
        """Create an Astrocyte instance from a config dictionary (for testing)."""
        from astrocyte.config import _dict_to_config, validate_astrocyte_config

        config = _dict_to_config(data)
        validate_astrocyte_config(config)
        return cls(config)

    def set_engine_provider(self, provider: EngineProvider) -> None:
        """Set the Tier 2 engine provider (for programmatic setup)."""
        from astrocyte.provider import check_spi_version

        check_spi_version(provider, "EngineProvider")
        self._engine_provider = provider
        self._dispatcher.engine_provider = provider
        if hasattr(provider, "capabilities"):
            self._dispatcher.capabilities = provider.capabilities()
        self._rebuild_tiered_retrieval()
        self._rebuild_multi_bank()

    def set_wiki_store(self, wiki_store: object) -> None:
        """Set the WikiStore provider (M8 wiki compile). Optional.

        When a WikiStore is configured, ``brain.compile()`` becomes available.
        Compile is disabled by default — banks that don't opt in keep today's
        retain/recall behaviour exactly.

        Args:
            wiki_store: Any object satisfying the
                :class:`~astrocyte.provider.WikiStore` protocol.
        """
        from astrocyte.provider import check_spi_version

        check_spi_version(wiki_store, "WikiStore")
        self._wiki_store = wiki_store

    def set_compile_queue(self, queue: object) -> None:
        """Set the async compile queue (M8 W4 threshold trigger). Optional.

        When a :class:`~astrocyte.pipeline.compile_trigger.CompileQueue` is
        configured, each successful ``brain.retain()`` call notifies the queue.
        The queue fires a background compile job whenever the bank crosses the
        configured size or staleness threshold.

        The queue must be started (``await queue.start()``) before the first
        retain call, and stopped (``await queue.stop()``) on shutdown.

        Args:
            queue: A :class:`~astrocyte.pipeline.compile_trigger.CompileQueue`
                instance (or any object with a compatible ``notify_retain``
                method).
        """
        self._compile_queue = queue

    def set_pipeline(self, pipeline: PipelineOrchestrator) -> None:
        """Set the Tier 1 pipeline orchestrator (for programmatic setup)."""
        self._pipeline = pipeline
        self._dispatcher.pipeline = pipeline
        from astrocyte.pipeline.extraction import merged_extraction_profiles

        pipeline.extraction_profiles = merged_extraction_profiles(self._config)
        pipeline.recall_authority = self._config.recall_authority
        # Wire the LLM provider to the MIP router for intent-layer escalation
        if self._mip_router and hasattr(pipeline, "llm_provider"):
            self._mip_router._llm_provider = pipeline.llm_provider
        # Expose router for per-bank MIP resolution at recall time (P3)
        pipeline.mip_router = self._mip_router
        self._rebuild_tiered_retrieval()
        self._rebuild_multi_bank()

    def _rebuild_tiered_retrieval(self) -> None:
        """Construct :class:`~astrocyte.pipeline.tiered_retrieval.TieredRetriever` when enabled."""
        from astrocyte.hybrid import HybridEngineProvider
        from astrocyte.pipeline.recall_cache import RecallCache
        from astrocyte.pipeline.recent_buffer import RecentMemoryBuffer
        from astrocyte.pipeline.tiered_retrieval import TieredRetriever

        self._dispatcher.tiered_retriever = None
        if not self._pipeline or not self._config.tiered_retrieval.enabled:
            return
        trc = self._config.tiered_retrieval
        if self._engine_provider is not None and trc.full_recall != "hybrid":
            return
        full_recall_fn = None
        if trc.full_recall == "hybrid":
            if not isinstance(self._engine_provider, HybridEngineProvider):
                logger.warning(
                    "tiered_retrieval.full_recall=hybrid requires HybridEngineProvider; "
                    "tiered retrieval disabled until a hybrid provider is set",
                )
                return
            full_recall_fn = self._engine_provider.recall
        rcc = self._config.recall_cache
        cache: RecallCache | None = None
        if rcc.enabled:
            cache = RecallCache(
                similarity_threshold=rcc.similarity_threshold,
                max_entries=rcc.max_entries,
                ttl_seconds=rcc.ttl_seconds,
            )
        recent = RecentMemoryBuffer()
        self._dispatcher.tiered_retriever = TieredRetriever(
            self._pipeline,
            recall_cache=cache,
            recent_buffer=recent,
            min_results=trc.min_results,
            min_score=trc.min_score,
            max_tier=trc.max_tier,
            full_recall=full_recall_fn,
        )

    def _rebuild_multi_bank(self) -> None:
        """Rebuild MultiBankOrchestrator when provider changes."""
        self._multi_bank = MultiBankOrchestrator(
            do_recall=self._dispatcher.recall,
            make_request=self._make_recall_request,
            circuit_breaker_record_failure=self._policy.record_failure,
            metrics=self._metrics,
            provider_name=self._dispatcher.provider_name,
        )

    def set_access_grants(self, grants: list[AccessGrant]) -> None:
        """Configure access grants."""
        self._policy.set_access_grants(grants)

    @property
    def _rate_limiters(self) -> dict:
        """Expose rate limiters for testing/introspection."""
        return self._policy._rate_limiters

    def register_hook(self, event_type: str, handler: HookHandler) -> None:
        """Register an event hook handler."""
        self._hook_manager.register(event_type, handler)

    # ---------------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------------

    async def retain(
        self,
        content: str,
        bank_id: str,
        *,
        metadata: dict[str, str | int | float | bool | None] | None = None,
        tags: list[str] | None = None,
        context: AstrocyteContext | None = None,
        content_type: str = "text",
        extraction_profile: str | None = None,
        occurred_at: datetime | None = None,
        source: str | None = None,
        pii_detected: bool = False,
    ) -> RetainResult:
        """Store content into memory.

        Args:
            content_type: ``text``, ``conversation``, ``document``, etc.
            extraction_profile: Name under YAML ``extraction_profiles:``.
            occurred_at: When the content originally occurred.
            source: Origin identifier for the content.
            pii_detected: Whether PII was already detected upstream.
        """
        validate_bank_id(bank_id)
        with span("astrocyte.retain", {"astrocyte.bank_id": bank_id}):
            # Input size validation — reject oversized content before pipeline processing
            input_error = self._policy.validate_retain_input(content, tags)
            if input_error:
                return RetainResult(stored=False, error=input_error)

            # Access control
            self._policy.check_access(bank_id, "write", context)

            # MIP routing (before policy layer)
            mip_pipeline = None
            mip_rule_name = None
            if self._mip_router:
                from astrocyte.identity import resolve_actor
                from astrocyte.mip.rule_engine import RuleEngineInput

                # Identity spec §3 Gap 2: resolved actor flows into MIP so
                # rules can branch on principal_type / principal_id / etc.
                # When no context is supplied (legacy callers), actor is
                # None and principal_* match conditions simply never fire.
                actor_identity = resolve_actor(context) if context else None

                mip_input = RuleEngineInput(
                    content=content,
                    content_type=content_type,
                    metadata=metadata,
                    tags=tags,
                    pii_detected=pii_detected,
                    source=source,
                    actor_identity=actor_identity,
                )
                routing = await self._mip_router.route(mip_input)
                if routing.bank_id:
                    # Re-check access for new bank
                    if routing.bank_id != bank_id:
                        self._policy.check_access(routing.bank_id, "write", context)
                    bank_id = routing.bank_id
                if routing.tags is not None:
                    tags = routing.tags
                if routing.retain_policy == "reject":
                    return RetainResult(stored=False, error="Rejected by MIP routing rule")
                mip_pipeline = routing.pipeline
                mip_rule_name = routing.rule_name

            # Rate limiting + quota (atomic to prevent TOCTOU)
            self._policy.check_rate_and_quota(bank_id, "retain")

            # Content validation
            errors = self._policy.validate_content(content, content_type)
            if errors:
                return RetainResult(stored=False, error="; ".join(errors))

            # PII scanning (async for LLM/rules_then_llm modes)
            content, pii_matches = await self._policy.scan_pii(content, self._config.barriers.pii.mode)
            if pii_matches:
                pii_action = self._policy.pii_action
                self._logger.log(
                    "astrocyte.policy.pii_detected",
                    bank_id=bank_id,
                    operation="retain",
                    data={"pii_types": ",".join(m.pii_type for m in pii_matches), "action": pii_action},
                )
                self._metrics.inc_counter(
                    "astrocyte_pii_detected_total",
                    {"bank_id": bank_id, "action": pii_action},
                )
                await self._hook_manager.fire(
                    "on_pii_detected",
                    bank_id=bank_id,
                    data={
                        "pii_types": ",".join(m.pii_type for m in pii_matches),
                        "action": pii_action,
                    },
                )

            # Metadata sanitization
            metadata, meta_warnings = self._policy.sanitize_metadata(metadata)

            # Build request
            request = RetainRequest(
                content=content,
                bank_id=bank_id,
                metadata=metadata,
                tags=tags,
                occurred_at=occurred_at,
                source=source,
                content_type=content_type,
                extraction_profile=extraction_profile,
                mip_pipeline=mip_pipeline,
                mip_rule_name=mip_rule_name,
            )

            # Route to provider
            try:
                self._policy.check_circuit(self._provider_name)
                with timed() as t:
                    result = await self._dispatcher.retain(request)
                self._policy.record_success()
                self._policy.record_quota(bank_id, "retain")
                self._metrics.inc_counter(
                    "astrocyte_retain_total",
                    {"bank_id": bank_id, "provider": self._provider_name, "status": "ok"},
                )
                self._metrics.observe_histogram(
                    "astrocyte_retain_duration_seconds",
                    t["elapsed_ms"] / 1000,
                    {"bank_id": bank_id, "provider": self._provider_name},
                )
                self._analytics.record_retain(
                    bank_id,
                    len(content),
                    deduplicated=getattr(result, "deduplicated", False),
                )
                await self._hook_manager.fire(
                    "on_retain",
                    bank_id=bank_id,
                    data={
                        "memory_id": result.memory_id or "",
                        "content_length": len(content),
                    },
                )
                # M8 W4: notify the compile queue so it can trigger a background
                # compile when the bank crosses the configured threshold.
                if self._compile_queue is not None and result.stored:
                    self._compile_queue.notify_retain(bank_id)  # type: ignore[union-attr]
                return result
            except ProviderUnavailable:
                self._policy.handle_degraded_retain(self._provider_name)
                return RetainResult(stored=False, error="Provider unavailable (degraded mode)")
            except Exception:
                self._policy.record_failure()
                self._metrics.inc_counter(
                    "astrocyte_retain_total",
                    {"bank_id": bank_id, "provider": self._provider_name, "status": "error"},
                )
                raise

    async def _make_recall_request(
        self,
        query: str,
        bank_id: str,
        max_results: int,
        max_tokens: int | None,
        tags: list[str] | None,
        params: RecallParams,
    ) -> RecallRequest:
        from astrocyte.recall.proxy import merge_manual_and_proxy_hits

        ext = await merge_manual_and_proxy_hits(
            self._config,
            query=query,
            bank_id=bank_id,
            manual=params.external_context,
            metrics=self._metrics,
        )
        return RecallRequest(
            query=query,
            bank_id=bank_id,
            max_results=max_results,
            max_tokens=max_tokens,
            tags=tags,
            fact_types=params.fact_types,
            time_range=params.time_range,
            include_sources=params.include_sources,
            layer_weights=params.layer_weights,
            detail_level=params.detail_level,
            external_context=ext,
            as_of=params.as_of,  # M9
        )

    async def recall(
        self,
        query: str,
        bank_id: str | None = None,
        *,
        banks: list[str] | None = None,
        strategy: Literal["cascade", "parallel", "first_match"] | MultiBankStrategy | None = None,
        max_results: int = 10,
        max_tokens: int | None = None,
        tags: list[str] | None = None,
        context: AstrocyteContext | None = None,
        external_context: list[MemoryHit] | None = None,
        fact_types: list[str] | None = None,
        time_range: tuple[datetime, datetime] | None = None,
        include_sources: bool = False,
        layer_weights: dict[str, float] | None = None,
        detail_level: str | None = None,
        as_of: datetime | None = None,
    ) -> RecallResult:
        """Retrieve relevant memories for a query.

        With multiple ``banks``, use ``strategy`` (or a :class:`MultiBankStrategy`) to choose
        ``parallel`` (default), ``cascade`` (widen until enough hits), or ``first_match``.

        Args:
            external_context: Extra :class:`MemoryHit` items merged with retrieval via RRF.
            fact_types: Filter by fact type (e.g. ``["preference", "event"]``).
            time_range: ``(start, end)`` datetime tuple to scope retrieval.
            include_sources: Include source metadata in results.
            layer_weights: Per-layer scoring weights for tiered retrieval.
            detail_level: Granularity hint (``"summary"`` / ``"full"``).

        When ``tiered_retrieval.enabled`` is set and a pipeline is configured, recall uses
        :class:`~astrocyte.pipeline.tiered_retrieval.TieredRetriever` for cheap tiers.
        """
        # Resolve bank(s)
        bank_ids = self._policy.resolve_read_bank_ids(bank_id, banks, context)

        max_tokens = max_tokens or self._config.homeostasis.recall_max_tokens

        with span("astrocyte.recall", {"astrocyte.bank_count": len(bank_ids), "astrocyte.bank_id": bank_ids[0] if len(bank_ids) == 1 else f"{bank_ids[0]}+{len(bank_ids)-1}"}):
            # Access control for all banks
            for bid in bank_ids:
                self._policy.check_access(bid, "read", context)

            # Rate limiting + quota (atomic per-bank)
            for bid in bank_ids:
                self._policy.check_rate_and_quota(bid, "recall")

            # Build typed params for internal helpers
            _rp = RecallParams(
                external_context=external_context,
                fact_types=fact_types,
                time_range=time_range,
                include_sources=include_sources,
                layer_weights=layer_weights,
                detail_level=detail_level,
                as_of=as_of,  # M9
            )

            # Single bank — direct
            if len(bank_ids) == 1:
                request = await self._make_recall_request(
                    query,
                    bank_ids[0],
                    max_results,
                    max_tokens,
                    tags,
                    _rp,
                )
                try:
                    self._policy.check_circuit(self._provider_name)
                    with timed() as t:
                        result = await self._dispatcher.recall(request)
                    self._policy.record_success()
                    self._metrics.inc_counter(
                        "astrocyte_recall_total",
                        {"bank_id": bank_ids[0], "provider": self._provider_name, "status": "ok"},
                    )
                    self._metrics.observe_histogram(
                        "astrocyte_recall_duration_seconds",
                        t["elapsed_ms"] / 1000,
                        {"bank_id": bank_ids[0], "provider": self._provider_name},
                    )
                    top_score = result.hits[0].score if result.hits else 0.0
                    self._analytics.record_recall(
                        bank_ids[0],
                        len(result.hits),
                        top_score,
                    )
                    await self._hook_manager.fire(
                        "on_recall",
                        bank_id=bank_ids[0],
                        data={
                            "query_length": len(query),
                            "result_count": len(result.hits),
                        },
                    )
                    if self._config.dlp.scan_recall_output:
                        result = self._output_scanner.scan_recall(result)
                    return apply_recall_authority(result, self._config.recall_authority)
                except ProviderUnavailable:
                    return self._policy.handle_degraded_recall(self._provider_name)
                except Exception:
                    self._policy.record_failure()
                    raise

            strat = _normalize_multi_bank_strategy(strategy)
            result = await self._multi_bank.recall(
                query,
                bank_ids,
                max_results,
                max_tokens,
                tags,
                _rp,
                strat,
            )
            if self._config.dlp.scan_recall_output:
                result = self._output_scanner.scan_recall(result)
            return apply_recall_authority(result, self._config.recall_authority)

    async def reflect(
        self,
        query: str,
        bank_id: str | None = None,
        *,
        banks: list[str] | None = None,
        strategy: Literal["cascade", "parallel", "first_match"] | MultiBankStrategy | None = None,
        max_tokens: int | None = None,
        tags: list[str] | None = None,
        context: AstrocyteContext | None = None,
        include_sources: bool = True,
        dispositions: Any | None = None,
    ) -> ReflectResult:
        """Synthesize an answer from memory.

        Args:
            tags: Filter recall by tags during reflect.
            include_sources: Include source memories in the result.
            dispositions: Emotional/tonal dispositions for synthesis.

        Supports multi-bank reflect: pass ``banks`` (and optionally ``strategy``) to
        recall across multiple banks and synthesize over the fused results.
        """
        # Resolve bank(s)
        bank_ids = self._policy.resolve_read_bank_ids(bank_id, banks, context)

        max_tokens = max_tokens or self._config.homeostasis.reflect_max_tokens
        primary_bank = bank_ids[0]

        with span("astrocyte.reflect", {"astrocyte.bank_count": len(bank_ids), "astrocyte.bank_id": bank_ids[0] if len(bank_ids) == 1 else f"{bank_ids[0]}+{len(bank_ids)-1}"}):
            # Access control for all banks
            for bid in bank_ids:
                self._policy.check_access(bid, "read", context)

            for bid in bank_ids:
                self._policy.check_rate_and_quota(bid, "reflect")

            # ── Single bank: delegate to provider/pipeline reflect ──
            if len(bank_ids) == 1:
                request = ReflectRequest(
                    query=query,
                    bank_id=primary_bank,
                    max_tokens=max_tokens,
                    include_sources=include_sources,
                    dispositions=dispositions,
                )
                try:
                    self._policy.check_circuit(self._provider_name)
                    with timed() as t:
                        result = await self._dispatcher.reflect(request)
                    self._policy.record_success()
                except ProviderUnavailable:
                    return ReflectResult(answer="Memory unavailable", sources=[])
                except Exception:
                    self._policy.record_failure()
                    raise

            # ── Multi-bank: recall across banks, then synthesize ──
            else:
                strat = _normalize_multi_bank_strategy(strategy)
                with timed() as t:
                    _rp = RecallParams(include_sources=include_sources)
                    recall_result = await self._multi_bank.recall(
                        query,
                        bank_ids,
                        max_results=20,  # Larger set for synthesis context
                        max_tokens=None,  # Budget applied after synthesis
                        tags=tags,
                        params=_rp,
                        strategy=strat,
                    )
                    auth_ctx: str | None = None
                    ra = self._config.recall_authority
                    if ra.enabled and ra.apply_to_reflect:
                        recall_result = apply_recall_authority(recall_result, ra)
                        auth_ctx = recall_result.authority_context
                    result = await self._dispatcher.reflect_from_hits(
                        query=query,
                        hits=recall_result.hits,
                        bank_id=primary_bank,
                        max_tokens=max_tokens,
                        dispositions=dispositions,
                        authority_context=auth_ctx,
                    )

            self._analytics.record_reflect(
                primary_bank,
                success=bool(result.answer.strip()),
            )
            self._policy.record_quota(primary_bank, "reflect")
            self._metrics.inc_counter(
                "astrocyte_reflect_total",
                {"bank_id": ",".join(bank_ids), "provider": self._provider_name, "status": "ok"},
            )
            self._metrics.observe_histogram(
                "astrocyte_reflect_duration_seconds",
                t["elapsed_ms"] / 1000,
                {"bank_id": ",".join(bank_ids), "provider": self._provider_name},
            )
            await self._hook_manager.fire(
                "on_reflect",
                bank_id=primary_bank,
                data={
                    "query_length": len(query),
                    "answer_length": len(result.answer),
                    "bank_count": len(bank_ids),
                },
            )
            if self._config.dlp.scan_reflect_output:
                result = self._output_scanner.scan_reflect(result)
            return result

    async def clear_bank(
        self,
        bank_id: str,
        *,
        context: AstrocyteContext | None = None,
    ) -> ForgetResult:
        """Delete all memories in a bank. Requires admin access if ACL enabled."""
        return await self.forget(bank_id, scope="all", context=context)

    async def forget(
        self,
        bank_id: str,
        *,
        memory_ids: list[str] | None = None,
        tags: list[str] | None = None,
        scope: str | None = None,
        context: AstrocyteContext | None = None,
        compliance: bool = False,
        reason: str | None = None,
        before_date: datetime | None = None,
    ) -> ForgetResult:
        """Remove memories.

        Args:
            scope: ``"all"`` to delete everything in a bank (requires admin).
            compliance: Bypass legal holds for right-to-forget (requires context).
            reason: Audit reason when ``compliance=True``.
            before_date: Delete memories created before this date.
        """
        validate_bank_id(bank_id)
        with span("astrocyte.forget", {"astrocyte.bank_id": bank_id}):
            # scope="all" requires admin permission
            if scope == "all":
                if self._config.access_control.enabled:
                    self._policy.check_access(bank_id, "admin", context)
            else:
                self._policy.check_access(bank_id, "forget", context)

            # Legal hold check — compliance=True bypasses for right-to-forget.
            # Even when access_control is disabled, compliance bypass requires
            # explicit context (caller must identify themselves).
            # MIP forget policy resolution (Phase 4) — runs BEFORE the existing
            # legal-hold check so that a rule with respect_legal_hold=True wins
            # over the caller's compliance bypass, and so audit logs fire even
            # on policy refusal.
            mip_forget = (
                self._mip_router.resolve_forget_for_bank(bank_id) if self._mip_router else None
            )
            if mip_forget is not None:
                # max_per_call: cap blast radius for selective deletes by id
                if (
                    mip_forget.max_per_call is not None
                    and memory_ids is not None
                    and len(memory_ids) > mip_forget.max_per_call
                ):
                    raise MipRoutingError(
                        f"forget rejected: {len(memory_ids)} memory_ids exceeds "
                        f"forget.max_per_call={mip_forget.max_per_call} for bank {bank_id!r}"
                    )

                # min_age_days: refuse forget when any targeted record is younger.
                # Best-effort: requires the engine to populate `_created_at` in
                # metadata at retain time. Records lacking this stamp are skipped
                # with a warning (degraded enforcement, not a hard failure).
                if (
                    mip_forget.min_age_days is not None
                    and mip_forget.min_age_days > 0
                    and memory_ids
                ):
                    too_young = await self._collect_too_young_ids(
                        bank_id, memory_ids, mip_forget.min_age_days,
                    )
                    if too_young:
                        raise MipRoutingError(
                            f"forget rejected: {len(too_young)} record(s) in {bank_id!r} "
                            f"younger than forget.min_age_days={mip_forget.min_age_days} "
                            f"({sorted(too_young)[:5]}{'...' if len(too_young) > 5 else ''})"
                        )

                # audit: emit a structured log line before any deletion occurs
                if mip_forget.audit in ("required", "recommended"):
                    self._logger.log(
                        "astrocyte.mip.forget.audit",
                        bank_id=bank_id,
                        data={
                            "mode": str(mip_forget.mode or "soft"),
                            "audit": str(mip_forget.audit),
                            "cascade": bool(mip_forget.cascade) if mip_forget.cascade is not None else True,
                            "memory_ids_count": len(memory_ids) if memory_ids else 0,
                            "scope": scope or "selective",
                            "actor": context_principal_label(context) if context else "anonymous",
                            "reason": str(reason) if reason else None,
                        },
                        level=logging.WARNING,
                    )

                # respect_legal_hold: when True, override the compliance bypass.
                # The MIP rule is the source of truth; compliance flag cannot
                # circumvent a rule that explicitly demands legal hold respect.
                if mip_forget.respect_legal_hold:
                    self._lifecycle.check_forget_allowed(bank_id)

            if not compliance:
                # Skip duplicate check if MIP already enforced legal hold above.
                if mip_forget is None or not mip_forget.respect_legal_hold:
                    self._lifecycle.check_forget_allowed(bank_id)
            else:
                if context is None:
                    raise AccessDenied("anonymous", bank_id, "compliance_forget")
                # Log compliance forget with actor identity for audit trail
                principal_label = context_principal_label(context)
                audit_reason = reason or "compliance_forget_request"
                self._logger.log(
                    "astrocyte.compliance.forget",
                    bank_id=bank_id,
                    data={
                        "actor": principal_label,
                        "reason": str(audit_reason),
                        "scope": scope or "selective",
                    },
                    level=logging.WARNING,
                )
                # When access control is enabled, also require admin permission
                if self._config.access_control.enabled:
                    self._policy.check_access(bank_id, "admin", context)

            # Soft-delete path (mip_forget.mode == "soft"): mark records with
            # `_deleted: true` instead of physically removing. Recall is
            # responsible for filtering them out. Falls through to hard delete
            # with a warning if the engine doesn't expose `soft_delete`.
            soft_mode = (
                mip_forget is not None
                and mip_forget.mode == "soft"
                and memory_ids is not None
            )
            if soft_mode:
                soft_fn = getattr(self._engine_provider, "soft_delete", None)
                if soft_fn is not None:
                    deleted = await soft_fn(bank_id, memory_ids)
                    await self._hook_manager.fire(
                        "on_forget",
                        bank_id=bank_id,
                        data={"deleted_count": deleted, "archived_count": 0, "soft": True},
                    )
                    return ForgetResult(deleted_count=deleted)
                logging.getLogger("astrocyte.mip").warning(
                    "forget.mode=soft requested for bank=%s but engine %s does not "
                    "implement soft_delete(); falling back to hard delete",
                    bank_id, type(self._engine_provider).__name__ if self._engine_provider else "pipeline",
                )

            request = ForgetRequest(
                bank_id=bank_id,
                memory_ids=memory_ids,
                tags=tags,
                before_date=before_date,
                scope=scope,
            )
            result = await self._dispatcher.forget(request)
            await self._hook_manager.fire(
                "on_forget",
                bank_id=bank_id,
                data={
                    "deleted_count": result.deleted_count,
                    "archived_count": result.archived_count,
                },
            )
            return result

    async def _collect_too_young_ids(
        self,
        bank_id: str,
        memory_ids: list[str],
        min_age_days: int,
    ) -> list[str]:
        """Return the subset of ``memory_ids`` younger than ``min_age_days``.

        Best-effort enforcement of ``forget.min_age_days``: relies on records
        carrying a ``_created_at`` ISO timestamp in metadata (stamped by the
        engine at retain time). Records lacking the stamp are skipped with a
        warning — degraded enforcement, not a hard failure, since older data
        predating the stamp shouldn't permanently block legitimate forgets.
        """
        from datetime import timedelta

        wanted = set(memory_ids)
        try:
            result = await self._dispatcher.recall(
                RecallRequest(query="*", bank_id=bank_id, max_results=10000),
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "min_age_days check skipped: recall failed for bank=%s: %s",
                bank_id, exc,
            )
            return []

        now = datetime.now(timezone.utc)
        threshold = timedelta(days=min_age_days)
        too_young: list[str] = []
        seen: set[str] = set()
        missing_stamp = 0

        for hit in result.hits:
            if hit.memory_id is None or hit.memory_id not in wanted:
                continue
            seen.add(hit.memory_id)
            stamp = (hit.metadata or {}).get("_created_at")
            if stamp is None:
                missing_stamp += 1
                continue
            if isinstance(stamp, str):
                try:
                    created_at = datetime.fromisoformat(stamp)
                except ValueError:
                    missing_stamp += 1
                    continue
            elif isinstance(stamp, datetime):
                created_at = stamp
            else:
                missing_stamp += 1
                continue
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            if (now - created_at) < threshold:
                too_young.append(hit.memory_id)

        if missing_stamp:
            mip_logger = logging.getLogger("astrocyte.mip")
            mip_logger.warning(
                "min_age_days enforcement degraded: %d/%d record(s) in bank=%s "
                "lack `_created_at` metadata and were skipped",
                missing_stamp, len(seen), bank_id,
            )
            # One increment per forget call that encountered unstamped records.
            # The warning log above carries the exact count; this counter
            # fires dashboards / alerts when the condition occurs at all.
            self._metrics.inc_counter(
                "astrocyte_forget_unstamped_records_total",
                {"bank_id": bank_id},
            )
        return too_young

    async def compile(
        self,
        bank_id: str,
        scope: str | None = None,
    ) -> CompileResult:
        """Compile raw memories into WikiPages for ``bank_id`` (M8).

        Synthesises a structured wiki page for each detected topic scope using
        an LLM. Compiled pages are stored in the WikiStore and embedded back
        into the VectorStore (``memory_layer="compiled"``) so the recall pipeline
        can surface them ahead of raw memory fragments.

        Args:
            bank_id: Bank to compile.
            scope: If provided, compile only memories tagged with this scope string.
                   If ``None``, trigger full scope discovery:

                   1. Tagged memories are grouped by tag (each tag = one page).
                   2. Untagged memories are clustered by embedding similarity
                      (DBSCAN); each cluster is labelled with a lightweight LLM
                      call. Noise points are held for the next compile cycle.

        Returns:
            :class:`~astrocyte.types.CompileResult` with pages created/updated,
            noise count, and token usage.

        Raises:
            :class:`~astrocyte.errors.ConfigError`: If no WikiStore has been
                configured (call ``set_wiki_store()`` before ``compile()``).
            :class:`~astrocyte.errors.ProviderUnavailable`: If the Tier 1 pipeline
                has not been configured.

        Example::

            # Explicit — compile memories tagged "incident-response"
            result = await brain.compile("eng-team", scope="incident-response")

            # Automatic — discover scopes from tags and embedding clusters
            result = await brain.compile("eng-team")

            print(result.pages_created, result.pages_updated, result.tokens_used)
        """
        validate_bank_id(bank_id)

        if self._wiki_store is None:
            raise ConfigError(
                "brain.compile() requires a WikiStore. "
                "Call brain.set_wiki_store(store) before compiling."
            )

        if self._pipeline is None:
            raise ProviderUnavailable(
                "brain.compile() requires a Tier 1 pipeline. "
                "Call brain.set_pipeline(pipeline) before compiling."
            )

        from astrocyte.pipeline.compile import CompileEngine

        engine = CompileEngine(
            vector_store=self._pipeline.vector_store,
            llm_provider=self._pipeline.llm_provider,
            wiki_store=self._wiki_store,  # type: ignore[arg-type]
        )

        with span("compile", {"bank_id": bank_id, "scope": scope or "auto"}):
            result = await engine.run(bank_id, scope=scope)

        if result.error:
            logger.warning(
                "compile failed for bank %s scope %s: %s",
                bank_id,
                scope or "auto",
                result.error,
            )
        else:
            logger.info(
                "compile complete bank=%s scope=%s pages_created=%d pages_updated=%d "
                "noise=%d tokens=%d elapsed_ms=%d",
                bank_id,
                scope or "auto",
                result.pages_created,
                result.pages_updated,
                result.noise_memories,
                result.tokens_used,
                result.elapsed_ms,
            )

        return result

    async def history(
        self,
        query: str,
        bank_id: str,
        as_of: datetime,
        *,
        max_results: int = 10,
        max_tokens: int | None = None,
        tags: list[str] | None = None,
    ) -> HistoryResult:
        """Reconstruct what the agent knew at a past point in time (M9 time travel).

        Returns memories that existed in *bank_id* at the moment *as_of* — i.e.
        only memories whose ``retained_at`` timestamp is on or before *as_of*.
        Memories retained after *as_of* are excluded, giving a faithful snapshot
        of the agent's knowledge at that instant.

        Args:
            query: The recall query to run against the historical snapshot.
            bank_id: Bank to query.
            as_of: UTC datetime.  Memories retained after this moment are hidden.
            max_results: Maximum number of hits to return.
            max_tokens: Optional token budget for the result set.
            tags: Optional tag filter (applied on top of the time filter).

        Returns:
            :class:`~astrocyte.types.HistoryResult` with hits and the ``as_of``
            timestamp embedded for traceability.

        Raises:
            ConfigError: If no pipeline is configured (no vector store to query).

        Example::

            from datetime import datetime, UTC
            snapshot = await brain.history(
                "What did we know about Alice?",
                bank_id="user-alice",
                as_of=datetime(2025, 1, 1, tzinfo=UTC),
            )
            for hit in snapshot.hits:
                print(hit.retained_at, hit.text)
        """
        recall_result = await self.recall(
            query,
            bank_id=bank_id,
            max_results=max_results,
            max_tokens=max_tokens,
            tags=tags,
            as_of=as_of,
        )
        return HistoryResult(
            hits=recall_result.hits,
            total_available=recall_result.total_available,
            truncated=recall_result.truncated,
            as_of=as_of,
            bank_id=bank_id,
            trace=recall_result.trace,
        )

    async def audit(
        self,
        scope: str,
        bank_id: str,
        *,
        max_memories: int = 50,
        max_tokens: int | None = None,
        tags: list[str] | None = None,
    ) -> AuditResult:
        """Identify knowledge gaps for a topic in a memory bank (M10 gap analysis).

        Recalls up to *max_memories* relevant memories for *scope*, then calls
        an LLM audit judge to assess what is missing or under-covered.  The
        result includes a list of :class:`~astrocyte.types.GapItem` objects and
        a ``coverage_score`` between 0 (empty bank) and 1 (comprehensive).

        This is a diagnostic operation — it does not modify any stored memory.
        It does consume LLM tokens proportional to the number of memories scanned.

        Args:
            scope: Natural-language description of the topic to audit
                (e.g. ``"Alice's employment history"``).
            bank_id: Bank to audit.
            max_memories: Maximum number of memories to retrieve and pass to
                the audit judge.  Defaults to ``50``.
            max_tokens: Optional token budget applied to retrieved memories
                before the judge call.
            tags: Optional tag filter to narrow which memories are retrieved.

        Returns:
            :class:`~astrocyte.types.AuditResult` with gaps and coverage score.

        Raises:
            ConfigError: If no pipeline is configured.

        Example::

            result = await brain.audit(
                "Alice's employment history",
                bank_id="user-alice",
            )
            print(f"Coverage: {result.coverage_score:.0%}")
            for gap in result.gaps:
                print(f"[{gap.severity}] {gap.topic}: {gap.reason}")
        """
        from astrocyte.pipeline.audit import run_audit

        recall_result = await self.recall(
            scope,
            bank_id=bank_id,
            max_results=max_memories,
            max_tokens=max_tokens,
            tags=tags,
        )

        pipeline = self._pipeline
        if pipeline is None:
            from astrocyte.exceptions import ConfigError
            raise ConfigError("No pipeline configured — call set_pipeline() first.")

        return await run_audit(
            scope=scope,
            bank_id=bank_id,
            memories=recall_result.hits,
            llm_provider=pipeline.llm_provider,
            trace=recall_result.trace,
        )

    async def health(self) -> HealthStatus:
        """Check system health."""
        with timed() as t:
            if self._engine_provider:
                status = await self._engine_provider.health()
            elif self._pipeline:
                status = await self._pipeline.vector_store.health()
            else:
                status = HealthStatus(healthy=True, message="No provider configured")
        status.latency_ms = t["elapsed_ms"]
        return status

    # ---------------------------------------------------------------------------
    # Lifecycle — legal hold + TTL
    # ---------------------------------------------------------------------------

    def set_legal_hold(self, bank_id: str, hold_id: str, reason: str, *, set_by: str = "user:api") -> LegalHold:
        """Place a bank under legal hold. Blocks forget() until released."""
        return self._lifecycle.set_legal_hold(bank_id, hold_id, reason, set_by=set_by)

    def release_legal_hold(self, bank_id: str, hold_id: str) -> bool:
        """Release a legal hold from a bank. Returns True if hold existed."""
        return self._lifecycle.release_legal_hold(bank_id, hold_id)

    def is_under_hold(self, bank_id: str) -> bool:
        """Check if bank is under legal hold."""
        return self._lifecycle.is_under_hold(bank_id)

    async def run_lifecycle(self, bank_id: str) -> LifecycleRunResult:
        """Run TTL lifecycle check on a bank. Scan memories, archive/delete as needed.

        Note: v1 treats "archive" as delete — no separate archive storage yet.
        The LifecycleAction.action distinguishes the reason (ttl_unretrieved vs ttl_expired)
        so callers can differentiate, but both result in deletion from the provider.
        """
        from astrocyte.types import LifecycleAction

        if not self._config.lifecycle.enabled:
            return LifecycleRunResult(archived_count=0, deleted_count=0, skipped_count=0, actions=[])

        now = datetime.now(timezone.utc)
        actions: list[LifecycleAction] = []
        to_delete: list[str] = []

        # Scan memories via paginated list_vectors (avoids query="*" + 10K limit)
        vector_store = self._pipeline.vector_store if self._pipeline else None
        if vector_store and hasattr(vector_store, "list_vectors"):
            scan_offset = 0
            scan_batch = 200
            while True:
                items = await vector_store.list_vectors(bank_id, offset=scan_offset, limit=scan_batch)
                if not items:
                    break
                for item in items:
                    created_at = item.metadata.get("_created_at") if item.metadata else None
                    last_recalled = item.metadata.get("_last_recalled_at") if item.metadata else None
                    if isinstance(created_at, str):
                        created_at = datetime.fromisoformat(created_at)
                    if isinstance(last_recalled, str):
                        last_recalled = datetime.fromisoformat(last_recalled)
                    action = self._lifecycle.evaluate_memory_ttl(
                        memory_id=item.id,
                        bank_id=bank_id,
                        created_at=created_at,
                        last_recalled_at=last_recalled,
                        tags=item.metadata.get("_tags", "").split(",") if item.metadata and item.metadata.get("_tags") else None,
                        fact_type=item.metadata.get("_fact_type") if item.metadata else None,
                        now=now,
                    )
                    actions.append(action)
                    if action.action in ("delete", "archive"):
                        to_delete.append(item.id)
                scan_offset += len(items)
                if len(items) < scan_batch:
                    break
                if scan_offset > 100_000:
                    logger.warning("Lifecycle scan capped at 100k vectors for bank %s", bank_id)
                    break
        else:
            # Fallback for engine-only or stores without list_vectors
            logger.warning(
                "Lifecycle scan using query='*' fallback for bank %s — "
                "results may be incomplete. Use a pipeline with list_vectors support for full coverage.",
                bank_id,
            )
            result = await self._dispatcher.recall(RecallRequest(query="*", bank_id=bank_id, max_results=10000))
            for hit in result.hits:
                created_at = hit.metadata.get("_created_at") if hit.metadata else None
                last_recalled = hit.metadata.get("_last_recalled_at") if hit.metadata else None
                if isinstance(created_at, str):
                    created_at = datetime.fromisoformat(created_at)
                if isinstance(last_recalled, str):
                    last_recalled = datetime.fromisoformat(last_recalled)
                action = self._lifecycle.evaluate_memory_ttl(
                    memory_id=hit.memory_id or "",
                    bank_id=bank_id,
                    created_at=created_at,
                    last_recalled_at=last_recalled,
                    tags=hit.tags,
                    fact_type=hit.fact_type,
                    now=now,
                )
                actions.append(action)
                if action.action in ("delete", "archive"):
                    to_delete.append(hit.memory_id or "")

        # Batch delete/archive
        deleted = 0
        if to_delete:
            forget_result = await self._dispatcher.forget(ForgetRequest(bank_id=bank_id, memory_ids=to_delete))
            deleted = forget_result.deleted_count

        archived = sum(1 for a in actions if a.action == "archive")
        skipped = sum(1 for a in actions if a.action == "keep")

        # Run dedup consolidation if pipeline has a vector store
        consolidation_removed = 0
        if self._pipeline and self._pipeline.vector_store:
            from astrocyte.pipeline.consolidation import run_consolidation

            cons_result = await run_consolidation(
                self._pipeline.vector_store,
                bank_id,
                graph_store=getattr(self._pipeline, "graph_store", None),
            )
            consolidation_removed = cons_result.duplicates_removed

        return LifecycleRunResult(
            archived_count=archived,
            deleted_count=deleted + consolidation_removed,
            skipped_count=skipped,
            actions=actions,
        )

    # ---------------------------------------------------------------------------
    # Bank health & analytics
    # ---------------------------------------------------------------------------

    async def bank_health(self, bank_id: str) -> "BankHealth":
        """Compute health score and issues for a bank.

        Uses in-memory operation counters collected since process start.
        Optionally enriches with memory count from the vector store.
        """
        counters = self._analytics.get_counters(bank_id)
        memory_count = 0
        if self._pipeline and self._pipeline.vector_store:
            try:
                items = await self._pipeline.vector_store.list_vectors(bank_id, limit=0)
                memory_count = len(items)
            except Exception:
                logger.debug(
                    "list_vectors(limit=0) failed or unsupported; bank_health memory_count=0",
                    exc_info=True,
                )
        return compute_bank_health(bank_id, counters, memory_count)

    async def all_bank_health(self) -> list["BankHealth"]:
        """Compute health for all banks that have recorded operations."""
        results = []
        for bid in self._analytics.bank_ids():
            results.append(await self.bank_health(bid))
        return results

    def bank_quality_snapshot(self, bank_id: str) -> "QualityDataPoint":
        """Return a QualityDataPoint snapshot of current counters for trend tracking."""
        counters = self._analytics.get_counters(bank_id)
        return counters_to_quality_point(counters)

    # ---------------------------------------------------------------------------
    # Memory portability
    # ---------------------------------------------------------------------------

    async def export_bank(
        self,
        bank_id: str,
        path: str,
        *,
        include_embeddings: bool = False,
        include_entities: bool = True,
        context: AstrocyteContext | None = None,
    ) -> int:
        """Export a memory bank to AMA (Astrocyte Memory Archive) JSONL format.

        Returns the number of memories exported.
        """
        from astrocyte.portability import export_bank as _export

        self._policy.check_access(bank_id, "admin", context)

        count = await _export(
            recall_fn=self._dispatcher.recall,
            bank_id=bank_id,
            path=path,
            provider_name=self._provider_name,
            include_embeddings=include_embeddings,
            include_entities=include_entities,
        )
        await self._hook_manager.fire("on_export", bank_id=bank_id, data={"memory_count": count, "path": path})
        return count

    async def import_bank(
        self,
        bank_id: str,
        path: str,
        *,
        on_conflict: str = "skip",
        context: AstrocyteContext | None = None,
        progress_fn: Any = None,
    ) -> Any:
        """Import memories from an AMA JSONL file into a bank.

        Returns an ImportResult with imported/skipped/errors counts.
        """
        from astrocyte.portability import ImportResult
        from astrocyte.portability import import_bank as _import

        self._policy.check_access(bank_id, "admin", context)

        result: ImportResult = await _import(
            retain_fn=self._dispatcher.retain,
            bank_id=bank_id,
            path=path,
            on_conflict=on_conflict,
            progress_fn=progress_fn,
        )
        await self._hook_manager.fire(
            "on_import",
            bank_id=bank_id,
            data={
                "imported": result.imported,
                "skipped": result.skipped,
                "errors": result.errors,
            },
        )
        return result

    # ---------------------------------------------------------------------------
    # Internal routing
    # ---------------------------------------------------------------------------

    @property
    def _provider_name(self) -> str:
        return self._config.provider or "pipeline"

    # ---------------------------------------------------------------------------
    # Backwards-compat thin wrappers (tests access these directly)
    # ---------------------------------------------------------------------------

    @property
    def _tiered_retriever(self):
        """Expose tiered retriever for testing/introspection."""
        return self._dispatcher.tiered_retriever

    async def _do_retain(self, request: RetainRequest) -> RetainResult:
        return await self._dispatcher.retain(request)

    async def _do_recall(self, request: RecallRequest) -> RecallResult:
        return await self._dispatcher.recall(request)

    async def _do_forget(self, request: ForgetRequest) -> ForgetResult:
        return await self._dispatcher.forget(request)

