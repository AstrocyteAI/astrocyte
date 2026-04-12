"""Policy enforcer — access control, rate limiting, PII scanning, input validation."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from astrocyte._validation import validate_bank_id
from astrocyte.config import AstrocyteConfig
from astrocyte.errors import AccessDenied, ConfigError, RateLimited
from astrocyte.identity import (
    BankResolver,
    accessible_read_banks,
    context_principal_label,
    effective_permissions,
)
from astrocyte.policy.barriers import ContentValidator, MetadataSanitizer, PiiScanner
from astrocyte.policy.escalation import CircuitBreaker, DegradedModeHandler
from astrocyte.policy.homeostasis import QuotaTracker, RateLimiter
from astrocyte.types import (
    AccessGrant,
    AstrocyteContext,
    PiiMatch,
    RecallResult,
    RetainResult,
)

if TYPE_CHECKING:
    from astrocyte.types import Metadata

class PolicyEnforcer:
    """Centralizes all policy enforcement: access control, rate limiting,
    PII scanning, content validation, metadata sanitization, circuit breaking,
    and input validation.
    """

    def __init__(self, config: AstrocyteConfig) -> None:
        self._config = config

        # PII scanner
        self._pii_scanner = PiiScanner(
            mode=config.barriers.pii.mode,
            action=config.barriers.pii.action,
            countries=config.barriers.pii.countries,
            type_overrides=config.barriers.pii.type_overrides,
        )

        # Content validation
        self._content_validator = ContentValidator(
            max_content_length=config.barriers.validation.max_content_length,
            reject_empty=config.barriers.validation.reject_empty_content,
            allowed_content_types=config.barriers.validation.allowed_content_types,
        )

        # Metadata sanitization
        self._metadata_sanitizer = MetadataSanitizer(
            blocked_keys=config.barriers.metadata.blocked_keys,
            max_size_bytes=config.barriers.metadata.max_metadata_size_bytes,
        )

        # Rate limiters (per operation)
        self._rate_limiters: dict[str, RateLimiter] = {}
        rl = config.homeostasis.rate_limits
        if rl.retain_per_minute:
            self._rate_limiters["retain"] = RateLimiter(rl.retain_per_minute)
        if rl.recall_per_minute:
            self._rate_limiters["recall"] = RateLimiter(rl.recall_per_minute)
        if rl.reflect_per_minute:
            self._rate_limiters["reflect"] = RateLimiter(rl.reflect_per_minute)

        # Quota tracker
        self._quota_tracker = QuotaTracker()
        self._quota_limits: dict[str, int | None] = {
            "retain": config.homeostasis.quotas.retain_per_day,
            "reflect": config.homeostasis.quotas.reflect_per_day,
        }

        # Atomic lock for rate + quota checks
        self._rate_quota_lock = threading.Lock()

        # Circuit breaker
        cb = config.escalation.circuit_breaker
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=cb.failure_threshold,
            recovery_timeout_seconds=cb.recovery_timeout_seconds,
            half_open_max_calls=cb.half_open_max_calls,
        )
        self._degraded_handler = DegradedModeHandler(mode=config.escalation.degraded_mode)

        # Access control grants
        self._access_grants: list[AccessGrant] = []

    # -- Access grants --

    def set_access_grants(self, grants: list[AccessGrant]) -> None:
        """Configure access grants."""
        self._access_grants = grants

    @property
    def access_grants(self) -> list[AccessGrant]:
        return self._access_grants

    # -- Access control --

    def check_access(self, bank_id: str, permission: str, context: AstrocyteContext | None) -> None:
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

    # -- Bank resolution --

    def make_bank_resolver(self) -> BankResolver:
        i = self._config.identity
        return BankResolver(
            user_prefix=i.user_bank_prefix,
            agent_prefix=i.agent_bank_prefix,
            service_prefix=i.service_bank_prefix,
        )

    def resolve_read_bank_ids(
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
                resolver=self.make_bank_resolver(),
            )
        if not bank_ids:
            raise ConfigError("Either bank_id or banks must be provided")
        for bid in bank_ids:
            validate_bank_id(bid)
        return bank_ids

    # -- Rate limiting + quota --

    def check_rate_and_quota(self, bank_id: str, operation: str) -> None:
        """Atomically check rate limit and quota under a shared lock."""
        with self._rate_quota_lock:
            self._check_rate_limit(bank_id, operation)
            self._check_quota(bank_id, operation)

    def _check_rate_limit(self, bank_id: str, operation: str) -> None:
        limiter = self._rate_limiters.get(operation)
        if limiter:
            limiter.check_and_record(bank_id, operation)

    def _check_quota(self, bank_id: str, operation: str) -> None:
        limit = self._quota_limits.get(operation)
        if not self._quota_tracker.check(bank_id, operation, limit):
            raise RateLimited(bank_id=bank_id, operation=operation)

    def record_quota(self, bank_id: str, operation: str) -> None:
        """Record a successful operation against the quota tracker."""
        self._quota_tracker.record(bank_id, operation)

    # -- PII scanning --

    async def scan_pii(
        self, content: str, mode: str
    ) -> tuple[str, list[PiiMatch]]:
        """Scan content for PII. Returns (possibly redacted content, matches)."""
        if mode in ("llm", "rules_then_llm"):
            return await self._pii_scanner.apply_async(content)
        return self._pii_scanner.apply(content)

    @property
    def pii_action(self) -> str:
        return self._pii_scanner.action

    def scan_pii_output(self, text: str) -> list[PiiMatch]:
        """Scan text for PII matches (for DLP output scanning)."""
        return self._pii_scanner.scan(text)

    # -- Content validation --

    def validate_content(self, content: str, content_type: str) -> list[str]:
        """Validate content. Returns list of error strings (empty = valid)."""
        return self._content_validator.validate(content, content_type)

    # -- Metadata sanitization --

    def sanitize_metadata(
        self, metadata: "Metadata | None"
    ) -> tuple["Metadata | None", list[str]]:
        """Sanitize metadata. Returns (sanitized metadata, warnings)."""
        return self._metadata_sanitizer.sanitize(metadata)

    # -- Circuit breaker --

    def check_circuit(self, provider_name: str) -> None:
        """Check circuit breaker. Raises ProviderUnavailable if open."""
        self._circuit_breaker.check(provider_name)

    def record_success(self) -> None:
        self._circuit_breaker.record_success()

    def record_failure(self) -> None:
        self._circuit_breaker.record_failure()

    def handle_degraded_retain(self, provider_name: str) -> RetainResult:
        self._degraded_handler.handle_retain(provider_name)
        return RetainResult(stored=False, error="Provider unavailable (degraded mode)")

    def handle_degraded_recall(self, provider_name: str) -> RecallResult:
        return self._degraded_handler.handle_recall(provider_name)

    # -- Input validation --

    def validate_retain_input(
        self, content: str, tags: list[str] | None
    ) -> str | None:
        """Validate retain input sizes. Returns error string or None if valid."""
        max_content_bytes = self._config.homeostasis.retain_max_content_bytes
        if max_content_bytes:
            size = len(content.encode("utf-8"))
            if size > max_content_bytes:
                return f"Content exceeds maximum size ({size} > {max_content_bytes} bytes)"
        if tags and len(tags) > 100:
            return f"Too many tags ({len(tags)} > 100)"
        return None
