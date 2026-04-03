"""Core Astrocyte class — the main entry point for the framework.

Handles tier routing, policy enforcement, capability negotiation,
multi-bank orchestration, and hook dispatch.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from astrocytes.config import AstrocyteConfig, load_config
from astrocytes.errors import (
    AccessDenied,
    CapabilityNotSupported,
    ConfigError,
    ProviderUnavailable,
)
from astrocytes.policy.barriers import ContentValidator, MetadataSanitizer, PiiScanner
from astrocytes.policy.escalation import CircuitBreaker, DegradedModeHandler
from astrocytes.policy.homeostasis import QuotaTracker, RateLimiter, enforce_token_budget
from astrocytes.policy.observability import StructuredLogger, span, timed
from astrocytes.types import (
    AccessGrant,
    AstrocyteContext,
    EngineCapabilities,
    ForgetRequest,
    ForgetResult,
    HealthStatus,
    MemoryHit,
    RecallRequest,
    RecallResult,
    ReflectRequest,
    ReflectResult,
    RetainRequest,
    RetainResult,
)

logger = logging.getLogger("astrocytes")


class Astrocyte:
    """The Astrocytes memory framework — unified API for AI agent memory.

    Usage:
        brain = Astrocyte.from_config("astrocytes.yaml")
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

        # Access control
        self._access_grants: list[AccessGrant] = []

        # Hooks
        self._hooks: dict[str, list[Any]] = {}

        # Provider state (set during initialization)
        self._engine_provider: Any | None = None
        self._pipeline: Any | None = None
        self._capabilities: EngineCapabilities | None = None

    @classmethod
    def from_config(cls, path: str | Path) -> "Astrocyte":
        """Create an Astrocyte instance from a YAML config file."""
        config = load_config(path)
        return cls(config)

    @classmethod
    def from_config_dict(cls, data: dict[str, Any]) -> "Astrocyte":
        """Create an Astrocyte instance from a config dictionary (for testing)."""
        from astrocytes.config import _dict_to_config

        config = _dict_to_config(data)
        return cls(config)

    def set_engine_provider(self, provider: Any) -> None:
        """Set the Tier 2 engine provider (for programmatic setup)."""
        self._engine_provider = provider
        if hasattr(provider, "capabilities"):
            self._capabilities = provider.capabilities()

    def set_pipeline(self, pipeline: Any) -> None:
        """Set the Tier 1 pipeline orchestrator (for programmatic setup)."""
        self._pipeline = pipeline

    def set_access_grants(self, grants: list[AccessGrant]) -> None:
        """Configure access grants."""
        self._access_grants = grants

    def register_hook(self, event_type: str, handler: Any) -> None:
        """Register an event hook handler."""
        if event_type not in self._hooks:
            self._hooks[event_type] = []
        self._hooks[event_type].append(handler)

    # ---------------------------------------------------------------------------
    # Access control
    # ---------------------------------------------------------------------------

    def _check_access(self, bank_id: str, permission: str, context: AstrocyteContext | None) -> None:
        """Check access control. Raises AccessDenied if denied."""
        if not self._config.access_control.enabled:
            return
        if context is None:
            if self._config.access_control.default_policy == "open":
                return
            raise AccessDenied("anonymous", bank_id, permission)

        principal = context.principal

        for grant in self._access_grants:
            bank_match = grant.bank_id == "*" or grant.bank_id == bank_id
            principal_match = grant.principal == "*" or grant.principal == principal
            if bank_match and principal_match and permission in grant.permissions:
                return

        # Check default policy
        if self._config.access_control.default_policy == "open":
            return

        raise AccessDenied(principal, bank_id, permission)

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
        with span("astrocytes.retain", {"astrocytes.bank_id": bank_id}):
            # Access control
            self._check_access(bank_id, "write", context)

            # Rate limiting
            self._check_rate_limit(bank_id, "retain")

            # Content validation
            errors = self._content_validator.validate(content, kwargs.get("content_type", "text"))
            if errors:
                return RetainResult(stored=False, error="; ".join(errors))

            # PII scanning
            content, pii_matches = self._pii_scanner.apply(content)
            if pii_matches:
                self._logger.log(
                    "astrocytes.policy.pii_detected",
                    bank_id=bank_id,
                    operation="retain",
                    data={"pii_types": ",".join(m.pii_type for m in pii_matches), "action": self._pii_scanner.action},
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
                result = await self._do_retain(request)
                self._circuit_breaker.record_success()
                return result
            except ProviderUnavailable:
                self._degraded_handler.handle_retain(self._provider_name)
                return RetainResult(stored=False, error="Provider unavailable (degraded mode)")
            except Exception:
                self._circuit_breaker.record_failure()
                raise

    async def recall(
        self,
        query: str,
        bank_id: str | None = None,
        *,
        banks: list[str] | None = None,
        max_results: int = 10,
        max_tokens: int | None = None,
        tags: list[str] | None = None,
        context: AstrocyteContext | None = None,
        **kwargs: Any,
    ) -> RecallResult:
        """Retrieve relevant memories for a query."""
        # Resolve bank(s)
        bank_ids = banks or ([bank_id] if bank_id else [])
        if not bank_ids:
            raise ConfigError("Either bank_id or banks must be provided")

        max_tokens = max_tokens or self._config.homeostasis.recall_max_tokens

        with span("astrocytes.recall", {"astrocytes.bank_id": ",".join(bank_ids)}):
            # Access control for all banks
            for bid in bank_ids:
                self._check_access(bid, "read", context)

            # Rate limiting (once, not per-bank)
            self._check_rate_limit(bank_ids[0], "recall")

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
                    result = await self._do_recall(request)
                    self._circuit_breaker.record_success()
                    return result
                except ProviderUnavailable:
                    return self._degraded_handler.handle_recall(self._provider_name)
                except Exception:
                    self._circuit_breaker.record_failure()
                    raise

            # Multi-bank — fan out and merge
            return await self._multi_bank_recall(query, bank_ids, max_results, max_tokens, tags, kwargs)

    async def reflect(
        self,
        query: str,
        bank_id: str,
        *,
        max_tokens: int | None = None,
        context: AstrocyteContext | None = None,
        **kwargs: Any,
    ) -> ReflectResult:
        """Synthesize an answer from memory."""
        max_tokens = max_tokens or self._config.homeostasis.reflect_max_tokens

        with span("astrocytes.reflect", {"astrocytes.bank_id": bank_id}):
            self._check_access(bank_id, "read", context)
            self._check_rate_limit(bank_id, "reflect")

            request = ReflectRequest(
                query=query,
                bank_id=bank_id,
                max_tokens=max_tokens,
                include_sources=kwargs.get("include_sources", True),
                dispositions=kwargs.get("dispositions"),
            )

            try:
                self._circuit_breaker.check(self._provider_name)
                result = await self._do_reflect(request)
                self._circuit_breaker.record_success()
                return result
            except ProviderUnavailable:
                return ReflectResult(answer="Memory unavailable", sources=[])
            except Exception:
                self._circuit_breaker.record_failure()
                raise

    async def forget(
        self,
        bank_id: str,
        *,
        memory_ids: list[str] | None = None,
        tags: list[str] | None = None,
        context: AstrocyteContext | None = None,
        **kwargs: Any,
    ) -> ForgetResult:
        """Remove memories."""
        with span("astrocytes.forget", {"astrocytes.bank_id": bank_id}):
            self._check_access(bank_id, "forget", context)

            request = ForgetRequest(
                bank_id=bank_id,
                memory_ids=memory_ids,
                tags=tags,
                before_date=kwargs.get("before_date"),
            )
            return await self._do_forget(request)

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
    # Internal routing
    # ---------------------------------------------------------------------------

    @property
    def _provider_name(self) -> str:
        return self._config.provider or "pipeline"

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
        if self._pipeline and request.memory_ids:
            count = await self._pipeline.vector_store.delete(request.memory_ids, request.bank_id)
            return ForgetResult(deleted_count=count)
        raise CapabilityNotSupported(self._provider_name, "forget")

    async def _multi_bank_recall(
        self,
        query: str,
        bank_ids: list[str],
        max_results: int,
        max_tokens: int | None,
        tags: list[str] | None,
        kwargs: dict[str, Any],
    ) -> RecallResult:
        """Multi-bank recall — fan out to each bank and merge results."""
        import asyncio

        tasks = [
            self._do_recall(
                RecallRequest(
                    query=query,
                    bank_id=bid,
                    max_results=max_results,
                    max_tokens=None,  # Apply budget after merge
                    tags=tags,
                    fact_types=kwargs.get("fact_types"),
                )
            )
            for bid in bank_ids
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Merge hits from all banks
        all_hits: list[MemoryHit] = []
        total_available = 0
        for result in results:
            if isinstance(result, RecallResult):
                all_hits.extend(result.hits)
                total_available += result.total_available

        # Sort by score descending
        all_hits.sort(key=lambda h: h.score, reverse=True)

        # Deduplicate by text content
        seen_texts: set[str] = set()
        deduped: list[MemoryHit] = []
        for hit in all_hits:
            if hit.text not in seen_texts:
                seen_texts.add(hit.text)
                deduped.append(hit)

        # Trim to max_results
        trimmed = deduped[:max_results]

        # Token budget
        truncated = False
        if max_tokens:
            trimmed, truncated = enforce_token_budget(trimmed, max_tokens)

        return RecallResult(
            hits=trimmed,
            total_available=total_available,
            truncated=truncated,
        )
