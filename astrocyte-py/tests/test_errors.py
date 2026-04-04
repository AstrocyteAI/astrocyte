"""Tests for astrocyte.errors — exception hierarchy."""

from astrocyte.errors import (
    AccessDenied,
    AstrocyteError,
    CapabilityNotSupported,
    ConfigError,
    CrossBorderViolation,
    LegalHoldActive,
    PiiRejected,
    ProviderUnavailable,
    RateLimited,
)


class TestExceptionHierarchy:
    def test_all_inherit_from_base(self):
        assert issubclass(ConfigError, AstrocyteError)
        assert issubclass(CapabilityNotSupported, AstrocyteError)
        assert issubclass(AccessDenied, AstrocyteError)
        assert issubclass(RateLimited, AstrocyteError)
        assert issubclass(ProviderUnavailable, AstrocyteError)
        assert issubclass(PiiRejected, AstrocyteError)
        assert issubclass(CrossBorderViolation, AstrocyteError)
        assert issubclass(LegalHoldActive, AstrocyteError)

    def test_capability_not_supported(self):
        e = CapabilityNotSupported("mystique", "reflect")
        assert e.provider == "mystique"
        assert e.capability == "reflect"
        assert "mystique" in str(e)
        assert "reflect" in str(e)

    def test_access_denied(self):
        e = AccessDenied("agent:bot", "bank-1", "write")
        assert e.principal == "agent:bot"
        assert e.bank_id == "bank-1"
        assert e.permission == "write"

    def test_rate_limited(self):
        e = RateLimited("bank-1", "retain", retry_after_seconds=5.0)
        assert e.retry_after_seconds == 5.0
        assert "retry after" in str(e)

    def test_rate_limited_no_retry(self):
        e = RateLimited("bank-1", "recall")
        assert e.retry_after_seconds is None
        assert "retry after" not in str(e)

    def test_provider_unavailable(self):
        e = ProviderUnavailable("mem0", reason="timeout")
        assert "mem0" in str(e)
        assert "timeout" in str(e)

    def test_pii_rejected(self):
        e = PiiRejected(["email", "phone"])
        assert "email" in str(e)
        assert e.pii_types == ["email", "phone"]

    def test_cross_border(self):
        e = CrossBorderViolation("eu", "us")
        assert e.from_zone == "eu"
        assert e.to_zone == "us"

    def test_legal_hold(self):
        e = LegalHoldActive("bank-1", "case-001")
        assert e.hold_id == "case-001"

    def test_catchable_as_base(self):
        try:
            raise AccessDenied("bot", "bank", "read")
        except AstrocyteError as e:
            assert isinstance(e, AccessDenied)
