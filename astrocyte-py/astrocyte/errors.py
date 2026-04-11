"""Astrocyte exception hierarchy."""

from __future__ import annotations


class AstrocyteError(Exception):
    """Base exception for all Astrocyte errors."""


class ConfigError(AstrocyteError):
    """Configuration is invalid or missing."""


class CapabilityNotSupported(AstrocyteError):
    """The provider does not support the requested capability."""

    def __init__(self, provider: str, capability: str) -> None:
        self.provider = provider
        self.capability = capability
        super().__init__(f"Provider '{provider}' does not support '{capability}'")


class AccessDenied(AstrocyteError):
    """Principal lacks required permission on bank."""

    def __init__(self, principal: str, bank_id: str, permission: str) -> None:
        self.principal = principal
        self.bank_id = bank_id
        self.permission = permission
        super().__init__(f"Principal '{principal}' denied '{permission}' on bank '{bank_id}'")


class RateLimited(AstrocyteError):
    """Request exceeds rate limit."""

    def __init__(self, bank_id: str, operation: str, retry_after_seconds: float | None = None) -> None:
        self.bank_id = bank_id
        self.operation = operation
        self.retry_after_seconds = retry_after_seconds
        msg = f"Rate limited: {operation} on bank '{bank_id}'"
        if retry_after_seconds is not None:
            msg += f" (retry after {retry_after_seconds:.1f}s)"
        super().__init__(msg)


class ProviderUnavailable(AstrocyteError):
    """Provider is unreachable or circuit breaker is open."""

    def __init__(self, provider: str, reason: str | None = None) -> None:
        self.provider = provider
        self.reason = reason
        msg = f"Provider '{provider}' unavailable"
        if reason:
            msg += f": {reason}"
        super().__init__(msg)


class PiiRejected(AstrocyteError):
    """Content rejected due to PII detection policy."""

    def __init__(self, pii_types: list[str]) -> None:
        self.pii_types = pii_types
        super().__init__(f"Content rejected: PII detected ({', '.join(pii_types)})")


class CrossBorderViolation(AstrocyteError):
    """Operation would violate data residency policy."""

    def __init__(self, from_zone: str, to_zone: str) -> None:
        self.from_zone = from_zone
        self.to_zone = to_zone
        super().__init__(f"Cross-border violation: {from_zone} → {to_zone}")


class MipRoutingError(AstrocyteError):
    """MIP routing configuration or evaluation error."""


class IngestError(AstrocyteError):
    """Inbound ingest (webhook, stream, …) rejected a payload or configuration."""


class LegalHoldActive(AstrocyteError):
    """Operation blocked because bank is under legal hold."""

    def __init__(self, bank_id: str, hold_id: str) -> None:
        self.bank_id = bank_id
        self.hold_id = hold_id
        super().__init__(f"Bank '{bank_id}' is under legal hold '{hold_id}'")
