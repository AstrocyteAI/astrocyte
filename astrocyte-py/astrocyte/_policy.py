"""Policy enforcer — access control, rate limiting, PII scanning, input validation."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from astrocyte._validation import validate_bank_id
from astrocyte.config import AstrocyteConfig, BarrierConfig, HomeostasisConfig
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
from astrocyte.policy.signal_quality import NoisyBankDetector
from astrocyte.types import (
    AccessGrant,
    AstrocyteContext,
    PiiMatch,
    RecallResult,
    RetainResult,
)

if TYPE_CHECKING:
    from astrocyte.types import Metadata


@dataclass
class _BankPolicy:
    """Barrier + homeostasis enforcement built from one resolved config pair."""

    homeostasis: HomeostasisConfig
    barriers: BarrierConfig
    pii_scanner: PiiScanner
    content_validator: ContentValidator
    metadata_sanitizer: MetadataSanitizer
    rate_limiters: dict[str, RateLimiter]
    quota_limits: dict[str, int | None]

    @classmethod
    def build(cls, homeostasis: HomeostasisConfig, barriers: BarrierConfig) -> _BankPolicy:
        rl = homeostasis.rate_limits
        per_minute = {
            "retain": rl.retain_per_minute,
            "recall": rl.recall_per_minute,
            "reflect": rl.reflect_per_minute,
        }
        return cls(
            homeostasis=homeostasis,
            barriers=barriers,
            pii_scanner=PiiScanner(
                mode=barriers.pii.mode,
                action=barriers.pii.action,
                countries=barriers.pii.countries,
                type_overrides=barriers.pii.type_overrides,
            ),
            content_validator=ContentValidator(
                max_content_length=barriers.validation.max_content_length,
                reject_empty=barriers.validation.reject_empty_content,
                allowed_content_types=barriers.validation.allowed_content_types,
            ),
            metadata_sanitizer=MetadataSanitizer(
                blocked_keys=barriers.metadata.blocked_keys,
                max_size_bytes=barriers.metadata.max_metadata_size_bytes,
            ),
            rate_limiters={op: RateLimiter(limit) for op, limit in per_minute.items() if limit},
            quota_limits={
                "retain": homeostasis.quotas.retain_per_day,
                "reflect": homeostasis.quotas.reflect_per_day,
            },
        )


@dataclass(frozen=True)
class NoisyBankVerdict:
    """Outcome of a noisy-bank check: why the bank is flagged (empty = not),
    whether that changed since its last check, and the configured action."""

    reasons: tuple[str, ...]
    changed: bool
    action: str


class PolicyEnforcer:
    """Centralizes all policy enforcement: access control, rate limiting,
    PII scanning, content validation, metadata sanitization, circuit breaking,
    and input validation.

    Barriers and homeostasis are per bank: a bank with its own
    ``banks.<id>.homeostasis`` / ``barriers`` (or a bank ``profile``) gets its
    own scanner, validator, sanitizer, rate limits and quotas; every other bank
    shares the top-level set.
    """

    def __init__(self, config: AstrocyteConfig) -> None:
        self._config = config

        self._default_policy = _BankPolicy.build(config.homeostasis, config.barriers)
        self._bank_policies: dict[str, _BankPolicy] = {
            bank_id: _BankPolicy.build(config.bank_homeostasis(bank_id), config.bank_barriers(bank_id))
            for bank_id, bank in (config.banks or {}).items()
            if bank.homeostasis is not None or bank.barriers is not None
        }
        # Top-level components under their historical names (introspection).
        self._pii_scanner = self._default_policy.pii_scanner
        self._content_validator = self._default_policy.content_validator
        self._metadata_sanitizer = self._default_policy.metadata_sanitizer
        self._rate_limiters = self._default_policy.rate_limiters
        self._quota_limits = self._default_policy.quota_limits

        # Quota tracker (keyed by bank, so one tracker serves every bank's limits)
        self._quota_tracker = QuotaTracker()

        # Noisy-bank detection (signal_quality.noisy_bank, per bank)
        self._noisy_banks = NoisyBankDetector()

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

    def _for(self, bank_id: str | None) -> _BankPolicy:
        return self._bank_policies.get(bank_id, self._default_policy) if bank_id else self._default_policy

    def _check_rate_limit(self, bank_id: str, operation: str) -> None:
        limiter = self._for(bank_id).rate_limiters.get(operation)
        if limiter:
            limiter.check_and_record(bank_id, operation)

    def _check_quota(self, bank_id: str, operation: str) -> None:
        limit = self._for(bank_id).quota_limits.get(operation)
        if not self._quota_tracker.check(bank_id, operation, limit):
            raise RateLimited(bank_id=bank_id, operation=operation)

    def record_quota(self, bank_id: str, operation: str) -> None:
        """Record a successful operation against the quota tracker."""
        self._quota_tracker.record(bank_id, operation)

    # -- PII scanning --

    async def scan_pii(
        self, content: str, mode: str | None = None, bank_id: str | None = None
    ) -> tuple[str, list[PiiMatch]]:
        """Scan content for PII under ``bank_id``'s barriers (top-level when
        ``None``). ``mode`` defaults to that bank's ``barriers.pii.mode``.
        Returns (possibly redacted content, matches)."""
        policy = self._for(bank_id)
        if (mode or policy.barriers.pii.mode) in ("llm", "rules_then_llm"):
            return await policy.pii_scanner.apply_async(content)
        return policy.pii_scanner.apply(content)

    @property
    def pii_action(self) -> str:
        return self._pii_scanner.action

    def pii_action_for(self, bank_id: str | None) -> str:
        """``barriers.pii.action`` in effect for ``bank_id``."""
        return self._for(bank_id).pii_scanner.action

    def scan_pii_output(self, text: str) -> list[PiiMatch]:
        """Scan text for PII matches (for DLP output scanning)."""
        return self._pii_scanner.scan(text)

    # -- Content validation --

    def validate_content(self, content: str, content_type: str, bank_id: str | None = None) -> list[str]:
        """Validate content under ``bank_id``'s barriers. Returns list of error strings (empty = valid)."""
        return self._for(bank_id).content_validator.validate(content, content_type)

    # -- Metadata sanitization --

    def sanitize_metadata(
        self, metadata: "Metadata | None", bank_id: str | None = None
    ) -> tuple["Metadata | None", list[str]]:
        """Sanitize metadata under ``bank_id``'s barriers. Returns (sanitized metadata, warnings)."""
        return self._for(bank_id).metadata_sanitizer.sanitize(metadata)

    # -- Noisy-bank detection --

    def check_noisy_bank(self, bank_id: str) -> NoisyBankVerdict | None:
        """Check ``bank_id`` against its ``signal_quality.noisy_bank`` settings.
        ``None`` when detection is disabled for the bank."""
        cfg = self._config.bank_signal_quality(bank_id).noisy_bank
        if not cfg.enabled:
            return None
        reasons, changed = self._noisy_banks.check(
            bank_id,
            retain_spike_multiplier=cfg.retain_spike_multiplier,
            min_avg_content_length=cfg.min_avg_content_length,
            max_dedup_rate=cfg.max_dedup_rate,
        )
        return NoisyBankVerdict(reasons=reasons, changed=changed, action=cfg.action)

    def record_retain_signal(self, bank_id: str, content_length: int, deduplicated: bool) -> None:
        """Feed one processed retain to noisy-bank detection."""
        self._noisy_banks.record(bank_id, content_length, deduplicated)

    # -- Token budgets --

    def token_budget(self, bank_ids: list[str], operation: str) -> int | None:
        """``homeostasis.<operation>_max_tokens`` across ``bank_ids``: the
        strictest budget any of them sets (``None`` when none sets one), since
        a multi-bank recall or reflect returns one merged result."""
        budgets = [getattr(self._config.bank_homeostasis(b), f"{operation}_max_tokens") for b in bank_ids]
        set_budgets = [b for b in budgets if b is not None]
        return min(set_budgets) if set_budgets else None

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

    def validate_retain_input(self, content: str, tags: list[str] | None, bank_id: str | None = None) -> str | None:
        """Validate retain input sizes under ``bank_id``'s homeostasis. Returns error string or None if valid."""
        max_content_bytes = self._for(bank_id).homeostasis.retain_max_content_bytes
        if max_content_bytes:
            size = len(content.encode("utf-8"))
            if size > max_content_bytes:
                return f"Content exceeds maximum size ({size} > {max_content_bytes} bytes)"
        if tags and len(tags) > 100:
            return f"Too many tags ({len(tags)} > 100)"
        return None
