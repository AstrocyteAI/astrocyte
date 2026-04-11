"""Core Astrocyte class — the main entry point for the framework.

Handles tier routing, policy enforcement, capability negotiation,
multi-bank orchestration, and hook dispatch.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from astrocyte.analytics import BankMetricsCollector, compute_bank_health, counters_to_quality_point
from astrocyte.config import AstrocyteConfig, load_config
from astrocyte.errors import (
    AccessDenied,
    CapabilityNotSupported,
    ConfigError,
    ProviderUnavailable,
    RateLimited,
)
from astrocyte.identity import BankResolver, accessible_read_banks, context_principal_label, effective_permissions
from astrocyte.lifecycle import LifecycleManager
from astrocyte.mip.router import MipRouter
from astrocyte.policy.barriers import ContentValidator, MetadataSanitizer, PiiScanner
from astrocyte.policy.escalation import CircuitBreaker, DegradedModeHandler
from astrocyte.policy.homeostasis import QuotaTracker, RateLimiter, enforce_token_budget
from astrocyte.policy.observability import MetricsCollector, StructuredLogger, span, timed
from astrocyte.types import (
    AccessGrant,
    AstrocyteContext,
    BankHealth,
    EngineCapabilities,
    ForgetRequest,
    ForgetResult,
    HealthStatus,
    HookEvent,
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

# Hook handler type — FFI-safe: takes a HookEvent, returns an awaitable or None.
HookHandler = Callable[[HookEvent], Awaitable[None] | None]

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

        # Policy layer
        self._pii_scanner = PiiScanner(
            mode=config.barriers.pii.mode,
            action=config.barriers.pii.action,
            countries=config.barriers.pii.countries,
            type_overrides=config.barriers.pii.type_overrides,
        )
        self._content_validator = ContentValidator(
            max_content_length=config.barriers.validation.max_content_length,
            reject_empty=config.barriers.validation.reject_empty_content,
            allowed_content_types=config.barriers.validation.allowed_content_types,
        )
        self._metadata_sanitizer = MetadataSanitizer(
            blocked_keys=config.barriers.metadata.blocked_keys,
            max_size_bytes=config.barriers.metadata.max_metadata_size_bytes,
        )
        self._quota_tracker = QuotaTracker()

        # Rate limiters (per operation)
        self._rate_limiters: dict[str, RateLimiter] = {}
        rl = config.homeostasis.rate_limits
        if rl.retain_per_minute:
            self._rate_limiters["retain"] = RateLimiter(rl.retain_per_minute)
        if rl.recall_per_minute:
            self._rate_limiters["recall"] = RateLimiter(rl.recall_per_minute)
        if rl.reflect_per_minute:
            self._rate_limiters["reflect"] = RateLimiter(rl.reflect_per_minute)

        # Circuit breaker
        cb = config.escalation.circuit_breaker
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=cb.failure_threshold,
            recovery_timeout_seconds=cb.recovery_timeout_seconds,
            half_open_max_calls=cb.half_open_max_calls,
        )
        self._degraded_handler = DegradedModeHandler(mode=config.escalation.degraded_mode)

        # Quotas (daily limits)
        self._quota_limits: dict[str, int | None] = {
            "retain": config.homeostasis.quotas.retain_per_day,
            "reflect": config.homeostasis.quotas.reflect_per_day,
        }

        # Metrics
        self._metrics = MetricsCollector(enabled=config.observability.prometheus_enabled)

        # Access control
        self._access_grants: list[AccessGrant] = []

        # Hooks — typed as HookHandler (not Any)
        self._hooks: dict[str, list[HookHandler]] = {}

        # DLP output scanner (always regex, independent of input PII config)
        self._dlp_scanner: PiiScanner | None = None
        if config.dlp.scan_recall_output or config.dlp.scan_reflect_output:
            self._dlp_scanner = PiiScanner(mode="regex", action=config.dlp.output_pii_action)

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

        # Provider state — typed as protocols (not Any)
        self._engine_provider: EngineProvider | None = None
        self._pipeline: PipelineOrchestrator | None = None
        self._capabilities: EngineCapabilities | None = None

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
        from astrocyte.config import _dict_to_config

        config = _dict_to_config(data)
        return cls(config)

    def set_engine_provider(self, provider: EngineProvider) -> None:
        """Set the Tier 2 engine provider (for programmatic setup)."""
        from astrocyte.provider import check_spi_version

        check_spi_version(provider, "EngineProvider")
        self._engine_provider = provider
        if hasattr(provider, "capabilities"):
            self._capabilities = provider.capabilities()

    def set_pipeline(self, pipeline: PipelineOrchestrator) -> None:
        """Set the Tier 1 pipeline orchestrator (for programmatic setup)."""
        self._pipeline = pipeline
        # Wire the LLM provider to the MIP router for intent-layer escalation
        if self._mip_router and hasattr(pipeline, "llm_provider"):
            self._mip_router._llm_provider = pipeline.llm_provider

    def set_access_grants(self, grants: list[AccessGrant]) -> None:
        """Configure access grants."""
        self._access_grants = grants

    def register_hook(self, event_type: str, handler: HookHandler) -> None:
        """Register an event hook handler."""
        if event_type not in self._hooks:
            self._hooks[event_type] = []
        self._hooks[event_type].append(handler)

    # ---------------------------------------------------------------------------
    # Hook dispatch
    # ---------------------------------------------------------------------------

    async def _fire_hooks(
        self,
        event_type: str,
        bank_id: str | None = None,
        data: dict[str, str | int | float | bool | None] | None = None,
    ) -> None:
        """Fire all registered hooks for an event type. Non-blocking, failures logged."""
        handlers = self._hooks.get(event_type, [])
        if not handlers:
            return
        event = HookEvent(
            event_id=uuid.uuid4().hex,
            type=event_type,
            timestamp=datetime.now(timezone.utc),
            bank_id=bank_id,
            data=data,
        )
        for handler in handlers:
            try:
                result = handler(event)
                if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                    await result
            except Exception:
                self._logger.log(
                    "astrocyte.hook.error",
                    bank_id=bank_id,
                    data={"event_type": event_type},
                    level=logging.WARNING,
                )

    # ---------------------------------------------------------------------------
    # Quota enforcement
    # ---------------------------------------------------------------------------

    def _check_quota(self, bank_id: str, operation: str) -> None:
        """Check daily quota. Raises RateLimited if exceeded."""
        limit = self._quota_limits.get(operation)
        if not self._quota_tracker.check(bank_id, operation, limit):
            raise RateLimited(bank_id=bank_id, operation=operation)

    # ---------------------------------------------------------------------------
    # Access control
    # ---------------------------------------------------------------------------

    def _make_bank_resolver(self) -> BankResolver:
        i = self._config.identity
        return BankResolver(
            user_prefix=i.user_bank_prefix,
            agent_prefix=i.agent_bank_prefix,
            service_prefix=i.service_bank_prefix,
        )

    def _resolve_read_bank_ids(
        self,
        bank_id: str | None,
        banks: list[str] | None,
        context: AstrocyteContext | None,
    ) -> list[str]:
        """Resolve bank list for recall/reflect; optional identity-driven auto-resolve."""
        bank_ids = banks or ([bank_id] if bank_id else [])
        if not bank_ids and self._config.identity.auto_resolve_banks and context is not None:
            known = list((self._config.banks or {}).keys())
            bank_ids = accessible_read_banks(
                context,
                self._access_grants,
                known_bank_ids=known or None,
                resolver=self._make_bank_resolver(),
            )
        if not bank_ids:
            raise ConfigError("Either bank_id or banks must be provided")
        return bank_ids

    def _check_access(self, bank_id: str, permission: str, context: AstrocyteContext | None) -> None:
        """Check access control. Raises AccessDenied if denied."""
        if not self._config.access_control.enabled:
            return
        if context is None:
            if self._config.access_control.default_policy == "open":
                return
            raise AccessDenied("anonymous", bank_id, permission)

        eff = effective_permissions(context, self._access_grants, bank_id)
        if permission in eff:
            return

        if self._config.access_control.default_policy == "open":
            return

        raise AccessDenied(context_principal_label(context), bank_id, permission)

    # ---------------------------------------------------------------------------
    # Rate limiting
    # ---------------------------------------------------------------------------

    def _check_rate_limit(self, bank_id: str, operation: str) -> None:
        """Check rate limit for operation."""
        limiter = self._rate_limiters.get(operation)
        if limiter:
            limiter.check_and_record(bank_id, operation)

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
        **kwargs: Any,
    ) -> RetainResult:
        """Store content into memory."""
        with span("astrocyte.retain", {"astrocyte.bank_id": bank_id}):
            # Access control
            self._check_access(bank_id, "write", context)

            # MIP routing (before policy layer)
            if self._mip_router:
                from astrocyte.mip.rule_engine import RuleEngineInput

                mip_input = RuleEngineInput(
                    content=content,
                    content_type=kwargs.get("content_type", "text"),
                    metadata=metadata,
                    tags=tags,
                    pii_detected=bool(kwargs.get("pii_detected")),
                    source=kwargs.get("source"),
                )
                routing = await self._mip_router.route(mip_input)
                if routing.bank_id:
                    # Re-check access for new bank
                    if routing.bank_id != bank_id:
                        self._check_access(routing.bank_id, "write", context)
                    bank_id = routing.bank_id
                if routing.tags is not None:
                    tags = routing.tags
                if routing.retain_policy == "reject":
                    return RetainResult(stored=False, error="Rejected by MIP routing rule")

            # Rate limiting + quota
            self._check_rate_limit(bank_id, "retain")
            self._check_quota(bank_id, "retain")

            # Content validation
            errors = self._content_validator.validate(content, kwargs.get("content_type", "text"))
            if errors:
                return RetainResult(stored=False, error="; ".join(errors))

            # PII scanning (async for LLM/rules_then_llm modes)
            if self._config.barriers.pii.mode in ("llm", "rules_then_llm"):
                content, pii_matches = await self._pii_scanner.apply_async(content)
            else:
                content, pii_matches = self._pii_scanner.apply(content)
            if pii_matches:
                self._logger.log(
                    "astrocyte.policy.pii_detected",
                    bank_id=bank_id,
                    operation="retain",
                    data={"pii_types": ",".join(m.pii_type for m in pii_matches), "action": self._pii_scanner.action},
                )
                self._metrics.inc_counter(
                    "astrocyte_pii_detected_total",
                    {"bank_id": bank_id, "action": self._pii_scanner.action},
                )
                await self._fire_hooks(
                    "on_pii_detected",
                    bank_id=bank_id,
                    data={
                        "pii_types": ",".join(m.pii_type for m in pii_matches),
                        "action": self._pii_scanner.action,
                    },
                )

            # Metadata sanitization
            metadata, meta_warnings = self._metadata_sanitizer.sanitize(metadata)

            # Build request
            request = RetainRequest(
                content=content,
                bank_id=bank_id,
                metadata=metadata,
                tags=tags,
                occurred_at=kwargs.get("occurred_at"),
                source=kwargs.get("source"),
                content_type=kwargs.get("content_type", "text"),
            )

            # Route to provider
            try:
                self._circuit_breaker.check(self._provider_name)
                with timed() as t:
                    result = await self._do_retain(request)
                self._circuit_breaker.record_success()
                self._quota_tracker.record(bank_id, "retain")
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
                    bank_id, len(content),
                    deduplicated=getattr(result, "deduplicated", False),
                )
                await self._fire_hooks(
                    "on_retain",
                    bank_id=bank_id,
                    data={
                        "memory_id": result.memory_id or "",
                        "content_length": len(content),
                    },
                )
                return result
            except ProviderUnavailable:
                self._degraded_handler.handle_retain(self._provider_name)
                return RetainResult(stored=False, error="Provider unavailable (degraded mode)")
            except Exception:
                self._circuit_breaker.record_failure()
                self._metrics.inc_counter(
                    "astrocyte_retain_total",
                    {"bank_id": bank_id, "provider": self._provider_name, "status": "error"},
                )
                raise

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
        **kwargs: Any,
    ) -> RecallResult:
        """Retrieve relevant memories for a query.

        With multiple ``banks``, use ``strategy`` (or a :class:`MultiBankStrategy`) to choose
        ``parallel`` (default), ``cascade`` (widen until enough hits), or ``first_match``.
        """
        # Resolve bank(s)
        bank_ids = self._resolve_read_bank_ids(bank_id, banks, context)

        max_tokens = max_tokens or self._config.homeostasis.recall_max_tokens

        with span("astrocyte.recall", {"astrocyte.bank_id": ",".join(bank_ids)}):
            # Access control for all banks
            for bid in bank_ids:
                self._check_access(bid, "read", context)

            # Rate limiting — check each bank (rate limits are per-bank)
            for bid in bank_ids:
                self._check_rate_limit(bid, "recall")

            # Single bank — direct
            if len(bank_ids) == 1:
                request = RecallRequest(
                    query=query,
                    bank_id=bank_ids[0],
                    max_results=max_results,
                    max_tokens=max_tokens,
                    tags=tags,
                    fact_types=kwargs.get("fact_types"),
                    time_range=kwargs.get("time_range"),
                    include_sources=kwargs.get("include_sources", False),
                )
                try:
                    self._circuit_breaker.check(self._provider_name)
                    with timed() as t:
                        result = await self._do_recall(request)
                    self._circuit_breaker.record_success()
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
                        bank_ids[0], len(result.hits), top_score,
                    )
                    await self._fire_hooks(
                        "on_recall",
                        bank_id=bank_ids[0],
                        data={
                            "query_length": len(query),
                            "result_count": len(result.hits),
                        },
                    )
                    if self._config.dlp.scan_recall_output:
                        result = self._scan_recall_output(result)
                    return result
                except ProviderUnavailable:
                    return self._degraded_handler.handle_recall(self._provider_name)
                except Exception:
                    self._circuit_breaker.record_failure()
                    raise

            strat = _normalize_multi_bank_strategy(strategy)
            result = await self._multi_bank_recall(
                query,
                bank_ids,
                max_results,
                max_tokens,
                tags,
                kwargs,
                strat,
            )
            if self._config.dlp.scan_recall_output:
                result = self._scan_recall_output(result)
            return result

    async def reflect(
        self,
        query: str,
        bank_id: str | None = None,
        *,
        banks: list[str] | None = None,
        strategy: Literal["cascade", "parallel", "first_match"] | MultiBankStrategy | None = None,
        max_tokens: int | None = None,
        context: AstrocyteContext | None = None,
        **kwargs: Any,
    ) -> ReflectResult:
        """Synthesize an answer from memory.

        Supports multi-bank reflect: pass ``banks`` (and optionally ``strategy``) to
        recall across multiple banks and synthesize over the fused results.
        """
        # Resolve bank(s)
        bank_ids = self._resolve_read_bank_ids(bank_id, banks, context)

        max_tokens = max_tokens or self._config.homeostasis.reflect_max_tokens
        primary_bank = bank_ids[0]

        with span("astrocyte.reflect", {"astrocyte.bank_id": ",".join(bank_ids)}):
            # Access control for all banks
            for bid in bank_ids:
                self._check_access(bid, "read", context)

            for bid in bank_ids:
                self._check_rate_limit(bid, "reflect")
                self._check_quota(bid, "reflect")

            # ── Single bank: delegate to provider/pipeline reflect ──
            if len(bank_ids) == 1:
                request = ReflectRequest(
                    query=query,
                    bank_id=primary_bank,
                    max_tokens=max_tokens,
                    include_sources=kwargs.get("include_sources", True),
                    dispositions=kwargs.get("dispositions"),
                )
                try:
                    self._circuit_breaker.check(self._provider_name)
                    with timed() as t:
                        result = await self._do_reflect(request)
                    self._circuit_breaker.record_success()
                except ProviderUnavailable:
                    return ReflectResult(answer="Memory unavailable", sources=[])
                except Exception:
                    self._circuit_breaker.record_failure()
                    raise

            # ── Multi-bank: recall across banks, then synthesize ──
            else:
                strat = _normalize_multi_bank_strategy(strategy)
                with timed() as t:
                    recall_result = await self._multi_bank_recall(
                        query,
                        bank_ids,
                        max_results=20,  # Larger set for synthesis context
                        max_tokens=None,  # Budget applied after synthesis
                        tags=kwargs.get("tags"),
                        kwargs=kwargs,
                        strategy=strat,
                    )
                    result = await self._do_reflect_from_hits(
                        query=query,
                        hits=recall_result.hits,
                        bank_id=primary_bank,
                        max_tokens=max_tokens,
                        dispositions=kwargs.get("dispositions"),
                    )

            self._analytics.record_reflect(
                primary_bank, success=bool(result.answer.strip()),
            )
            self._quota_tracker.record(primary_bank, "reflect")
            self._metrics.inc_counter(
                "astrocyte_reflect_total",
                {"bank_id": ",".join(bank_ids), "provider": self._provider_name, "status": "ok"},
            )
            self._metrics.observe_histogram(
                "astrocyte_reflect_duration_seconds",
                t["elapsed_ms"] / 1000,
                {"bank_id": ",".join(bank_ids), "provider": self._provider_name},
            )
            await self._fire_hooks(
                "on_reflect",
                bank_id=primary_bank,
                data={
                    "query_length": len(query),
                    "answer_length": len(result.answer),
                    "bank_count": len(bank_ids),
                },
            )
            if self._config.dlp.scan_reflect_output:
                result = self._scan_reflect_output(result)
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
        **kwargs: Any,
    ) -> ForgetResult:
        """Remove memories.

        Use ``scope="all"`` to delete everything in a bank (requires admin).
        """
        with span("astrocyte.forget", {"astrocyte.bank_id": bank_id}):
            # scope="all" requires admin permission
            if scope == "all":
                if self._config.access_control.enabled:
                    self._check_access(bank_id, "admin", context)
            else:
                self._check_access(bank_id, "forget", context)

            # Legal hold check — compliance=True bypasses for right-to-forget.
            # Even when access_control is disabled, compliance bypass requires
            # explicit context (caller must identify themselves).
            if not kwargs.get("compliance"):
                self._lifecycle.check_forget_allowed(bank_id)
            else:
                if context is None:
                    from astrocyte.errors import AccessDenied

                    raise AccessDenied("anonymous", bank_id, "compliance_forget")
                # When access control is enabled, also require admin permission
                if self._config.access_control.enabled:
                    self._check_access(bank_id, "admin", context)

            request = ForgetRequest(
                bank_id=bank_id,
                memory_ids=memory_ids,
                tags=tags,
                before_date=kwargs.get("before_date"),
                scope=scope,
            )
            result = await self._do_forget(request)
            await self._fire_hooks(
                "on_forget",
                bank_id=bank_id,
                data={
                    "deleted_count": result.deleted_count,
                    "archived_count": result.archived_count,
                },
            )
            return result

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

        # Scan all memories in bank
        result = await self._do_recall(RecallRequest(query="*", bank_id=bank_id, max_results=10000))

        for hit in result.hits:
            created_at = hit.metadata.get("_created_at") if hit.metadata else None
            last_recalled = hit.metadata.get("_last_recalled_at") if hit.metadata else None

            # Parse datetime strings if needed
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
            forget_result = await self._do_forget(ForgetRequest(bank_id=bank_id, memory_ids=to_delete))
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
                pass  # list_vectors with limit=0 may not be supported; that's fine
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

        self._check_access(bank_id, "admin", context)

        count = await _export(
            recall_fn=self._do_recall,
            bank_id=bank_id,
            path=path,
            provider_name=self._provider_name,
            include_embeddings=include_embeddings,
            include_entities=include_entities,
        )
        await self._fire_hooks("on_export", bank_id=bank_id, data={"memory_count": count, "path": path})
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

        self._check_access(bank_id, "admin", context)

        result: ImportResult = await _import(
            retain_fn=self._do_retain,
            bank_id=bank_id,
            path=path,
            on_conflict=on_conflict,
            progress_fn=progress_fn,
        )
        await self._fire_hooks(
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
    # DLP output scanning
    # ---------------------------------------------------------------------------

    def _scan_recall_output(self, result: RecallResult) -> RecallResult:
        """Scan recall hits for PII. Redact/warn/reject per DLP config."""
        if not self._dlp_scanner:
            return result
        action = self._config.dlp.output_pii_action
        scanned_hits: list[MemoryHit] = []
        for hit in result.hits:
            matches = self._dlp_scanner.scan(hit.text)
            if not matches:
                scanned_hits.append(hit)
                continue
            if action == "reject":
                continue  # Drop hit silently
            if action == "redact":
                redacted, _ = self._dlp_scanner.apply(hit.text)
                scanned_hits.append(
                    MemoryHit(
                        text=redacted,
                        score=hit.score,
                        fact_type=hit.fact_type,
                        metadata=hit.metadata,
                        tags=hit.tags,
                        occurred_at=hit.occurred_at,
                        source=hit.source,
                        memory_id=hit.memory_id,
                        bank_id=hit.bank_id,
                        memory_layer=hit.memory_layer,
                        utility_score=hit.utility_score,
                    )
                )
            else:
                # warn — pass through with logging
                self._logger.log(
                    "astrocyte.dlp.recall_pii_detected",
                    bank_id=hit.bank_id or "",
                    operation="recall",
                    data={"pii_types": ",".join(m.pii_type for m in matches), "memory_id": hit.memory_id or ""},
                )
                scanned_hits.append(hit)

        return RecallResult(
            hits=scanned_hits,
            total_available=result.total_available,
            truncated=result.truncated,
            trace=result.trace,
        )

    def _scan_reflect_output(self, result: ReflectResult) -> ReflectResult:
        """Scan reflect answer for PII. Redact/warn/reject per DLP config."""
        if not self._dlp_scanner:
            return result
        matches = self._dlp_scanner.scan(result.answer)
        if not matches:
            return result

        action = self._config.dlp.output_pii_action
        if action == "reject":
            return ReflectResult(
                answer="",
                confidence=None,
                sources=result.sources,
                observations=["Reflect output blocked by DLP policy: PII detected"],
            )
        if action == "redact":
            redacted, _ = self._dlp_scanner.apply(result.answer)
            return ReflectResult(
                answer=redacted,
                confidence=result.confidence,
                sources=result.sources,
                observations=result.observations,
            )
        # warn
        self._logger.log(
            "astrocyte.dlp.reflect_pii_detected",
            operation="reflect",
            data={"pii_types": ",".join(m.pii_type for m in matches)},
        )
        return result

    # ---------------------------------------------------------------------------
    # Provider dispatch
    # ---------------------------------------------------------------------------

    async def _do_retain(self, request: RetainRequest) -> RetainResult:
        if self._engine_provider:
            return await self._engine_provider.retain(request)
        if self._pipeline:
            return await self._pipeline.retain(request)
        raise ConfigError("No provider or pipeline configured")

    async def _do_recall(self, request: RecallRequest) -> RecallResult:
        if self._engine_provider:
            return await self._engine_provider.recall(request)
        if self._pipeline:
            return await self._pipeline.recall(request)
        raise ConfigError("No provider or pipeline configured")

    async def _do_reflect(self, request: ReflectRequest) -> ReflectResult:
        # Check if provider supports reflect
        if self._engine_provider:
            if self._capabilities and self._capabilities.supports_reflect:
                return await self._engine_provider.reflect(request)
            # Fallback
            if self._config.fallback_strategy == "error":
                raise CapabilityNotSupported(self._provider_name, "reflect")
            if self._config.fallback_strategy == "degrade":
                # Return recall results as-is
                recall_result = await self._do_recall(
                    RecallRequest(query=request.query, bank_id=request.bank_id, max_results=10)
                )
                return ReflectResult(
                    answer="\n".join(h.text for h in recall_result.hits),
                    sources=recall_result.hits,
                )
            # local_llm fallback needs pipeline's reflect
            if self._pipeline:
                return await self._pipeline.reflect(request)
            raise CapabilityNotSupported(self._provider_name, "reflect")

        if self._pipeline:
            return await self._pipeline.reflect(request)

        raise ConfigError("No provider or pipeline configured")

    async def _do_forget(self, request: ForgetRequest) -> ForgetResult:
        if self._engine_provider:
            if self._capabilities and self._capabilities.supports_forget:
                return await self._engine_provider.forget(request)
            raise CapabilityNotSupported(self._provider_name, "forget")
        # Pipeline: delete from vector store
        if self._pipeline:
            if request.scope == "all" and hasattr(self._pipeline.vector_store, "list_vectors"):
                # Delete all vectors in bank by paginating through them
                total_deleted = 0
                while True:
                    batch = await self._pipeline.vector_store.list_vectors(request.bank_id, offset=0, limit=100)
                    if not batch:
                        break
                    ids = [v.id for v in batch]
                    total_deleted += await self._pipeline.vector_store.delete(ids, request.bank_id)
                return ForgetResult(deleted_count=total_deleted)
            if request.memory_ids:
                count = await self._pipeline.vector_store.delete(request.memory_ids, request.bank_id)
                return ForgetResult(deleted_count=count)
        raise CapabilityNotSupported(self._provider_name, "forget")

    async def _do_reflect_from_hits(
        self,
        query: str,
        hits: list[MemoryHit],
        bank_id: str,
        max_tokens: int | None = None,
        dispositions: Any = None,
    ) -> ReflectResult:
        """Synthesize over pre-fetched hits (used by multi-bank reflect).

        Tries in order:
        1. Pipeline reflect (if available) — builds a ReflectRequest, recall is skipped
           because we already have hits, so we call synthesize() directly.
        2. Engine reflect with degrade fallback — concatenate hit texts.
        3. Raise if nothing can synthesize.
        """
        # If we have a pipeline with an LLM, use its synthesis directly
        if self._pipeline:
            from astrocyte.pipeline.reflect import synthesize

            return await synthesize(
                query=query,
                hits=hits,
                llm_provider=self._pipeline.llm_provider,
                dispositions=dispositions,
                max_tokens=max_tokens or 2048,
            )

        # If engine supports reflect, we can't easily pass pre-fetched hits to it,
        # so fall back to degrade mode: concatenate hit texts as the answer.
        if hits:
            return ReflectResult(
                answer="\n".join(h.text for h in hits),
                sources=hits,
            )

        return ReflectResult(answer="No relevant memories found across banks.", sources=[])

    async def _multi_bank_recall(
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
            return await self._multi_bank_recall_parallel(
                query, bank_ids, max_results, max_tokens, tags, kwargs, strategy
            )
        if strategy.mode == "cascade":
            return await self._multi_bank_recall_cascade(
                query, bank_ids, max_results, max_tokens, tags, kwargs, strategy
            )
        if strategy.mode == "first_match":
            return await self._multi_bank_recall_first_match(
                query, bank_ids, max_results, max_tokens, tags, kwargs, strategy
            )
        raise ConfigError(f"Unknown multi-bank mode: {strategy.mode!r}")

    async def _multi_bank_recall_parallel(
        self,
        query: str,
        bank_ids: list[str],
        max_results: int,
        max_tokens: int | None,
        tags: list[str] | None,
        kwargs: dict[str, Any],
        strategy: MultiBankStrategy,
    ) -> RecallResult:
        tasks = [
            self._do_recall(
                RecallRequest(
                    query=query,
                    bank_id=bid,
                    max_results=max_results,
                    max_tokens=None,
                    tags=tags,
                    fact_types=kwargs.get("fact_types"),
                )
            )
            for bid in bank_ids
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_hits: list[MemoryHit] = []
        total_available = 0
        for bid, result in zip(bank_ids, results):
            if isinstance(result, RecallResult):
                all_hits.extend(_tag_hits_with_bank(result.hits, bid))
                total_available += result.total_available
            elif isinstance(result, BaseException):
                logger.warning("Multi-bank recall failed for bank '%s': %s", bid, result)

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

    async def _multi_bank_recall_cascade(
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
                RecallRequest(
                    query=query,
                    bank_id=bid,
                    max_results=max_results,
                    max_tokens=None,
                    tags=tags,
                    fact_types=kwargs.get("fact_types"),
                )
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

    async def _multi_bank_recall_first_match(
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
                RecallRequest(
                    query=query,
                    bank_id=bid,
                    max_results=max_results,
                    max_tokens=None,
                    tags=tags,
                    fact_types=kwargs.get("fact_types"),
                )
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
