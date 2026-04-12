"""Unit tests for PolicyEnforcer — access control, rate limiting, validation in isolation."""

from __future__ import annotations

import pytest

from astrocyte._policy import PolicyEnforcer
from astrocyte.config import AstrocyteConfig
from astrocyte.errors import AccessDenied, ConfigError, RateLimited
from astrocyte.types import AccessGrant, AstrocyteContext


def _make_enforcer(
    *,
    acl_enabled: bool = False,
    default_policy: str = "deny",
    pii_mode: str = "disabled",
    retain_per_minute: int | None = None,
    retain_per_day: int | None = None,
    max_content_bytes: int | None = None,
) -> PolicyEnforcer:
    config = AstrocyteConfig()
    config.access_control.enabled = acl_enabled
    config.access_control.default_policy = default_policy
    config.barriers.pii.mode = pii_mode
    config.homeostasis.rate_limits.retain_per_minute = retain_per_minute
    config.homeostasis.quotas.retain_per_day = retain_per_day
    config.homeostasis.retain_max_content_bytes = max_content_bytes
    return PolicyEnforcer(config)


class TestAccessControl:
    def test_disabled_acl_allows_everything(self) -> None:
        p = _make_enforcer(acl_enabled=False)
        # Should not raise
        p.check_access("any-bank", "write", None)

    def test_anonymous_denied_when_policy_deny(self) -> None:
        p = _make_enforcer(acl_enabled=True, default_policy="deny")
        with pytest.raises(AccessDenied):
            p.check_access("bank-1", "write", None)

    def test_anonymous_allowed_when_policy_open(self) -> None:
        p = _make_enforcer(acl_enabled=True, default_policy="open")
        p.check_access("bank-1", "write", None)  # Should not raise

    def test_granted_permission_allowed(self) -> None:
        p = _make_enforcer(acl_enabled=True, default_policy="deny")
        p.set_access_grants([
            AccessGrant(bank_id="bank-1", principal="user:alice", permissions=["read", "write"]),
        ])
        ctx = AstrocyteContext(principal="user:alice")
        p.check_access("bank-1", "write", ctx)  # Should not raise

    def test_missing_permission_denied(self) -> None:
        p = _make_enforcer(acl_enabled=True, default_policy="deny")
        p.set_access_grants([
            AccessGrant(bank_id="bank-1", principal="user:alice", permissions=["read"]),
        ])
        ctx = AstrocyteContext(principal="user:alice")
        with pytest.raises(AccessDenied):
            p.check_access("bank-1", "write", ctx)

    def test_wildcard_principal(self) -> None:
        p = _make_enforcer(acl_enabled=True, default_policy="deny")
        p.set_access_grants([
            AccessGrant(bank_id="bank-1", principal="*", permissions=["read"]),
        ])
        ctx = AstrocyteContext(principal="user:anyone")
        p.check_access("bank-1", "read", ctx)  # Should not raise


class TestResolveBankIds:
    def test_explicit_bank_id(self) -> None:
        p = _make_enforcer()
        assert p.resolve_read_bank_ids("bank-1", None, None) == ["bank-1"]

    def test_explicit_banks_list(self) -> None:
        p = _make_enforcer()
        assert p.resolve_read_bank_ids(None, ["a", "b"], None) == ["a", "b"]

    def test_no_bank_raises(self) -> None:
        p = _make_enforcer()
        with pytest.raises(ConfigError, match="bank_id or banks"):
            p.resolve_read_bank_ids(None, None, None)

    def test_invalid_bank_id_raises(self) -> None:
        p = _make_enforcer()
        with pytest.raises(ConfigError, match="Invalid bank_id"):
            p.resolve_read_bank_ids("has space", None, None)


class TestRateLimiting:
    def test_rate_limit_allows_within_limit(self) -> None:
        p = _make_enforcer(retain_per_minute=5)
        for _ in range(5):
            p.check_rate_and_quota("b1", "retain")

    def test_rate_limit_blocks_over_limit(self) -> None:
        p = _make_enforcer(retain_per_minute=2)
        p.check_rate_and_quota("b1", "retain")
        p.check_rate_and_quota("b1", "retain")
        with pytest.raises(RateLimited):
            p.check_rate_and_quota("b1", "retain")

    def test_no_rate_limit_configured(self) -> None:
        p = _make_enforcer(retain_per_minute=None)
        # Should not raise even with many calls
        for _ in range(100):
            p.check_rate_and_quota("b1", "retain")


class TestQuota:
    def test_quota_blocks_after_limit(self) -> None:
        p = _make_enforcer(retain_per_day=2)
        p.record_quota("b1", "retain")
        p.record_quota("b1", "retain")
        with pytest.raises(RateLimited):
            p.check_rate_and_quota("b1", "retain")


class TestInputValidation:
    def test_content_within_limit(self) -> None:
        p = _make_enforcer(max_content_bytes=1000)
        assert p.validate_retain_input("hello", None) is None

    def test_content_exceeds_limit(self) -> None:
        p = _make_enforcer(max_content_bytes=10)
        error = p.validate_retain_input("x" * 100, None)
        assert error is not None
        assert "exceeds maximum" in error

    def test_too_many_tags(self) -> None:
        p = _make_enforcer()
        error = p.validate_retain_input("hi", tags=[f"t{i}" for i in range(101)])
        assert error is not None
        assert "Too many tags" in error

    def test_no_limit_configured(self) -> None:
        p = _make_enforcer(max_content_bytes=None)
        assert p.validate_retain_input("x" * 1_000_000, None) is None


class TestContentValidation:
    def test_empty_content_rejected(self) -> None:
        p = _make_enforcer()
        errors = p.validate_content("", "text")
        assert len(errors) > 0

    def test_valid_content_accepted(self) -> None:
        p = _make_enforcer()
        errors = p.validate_content("hello world", "text")
        assert errors == []


class TestMetadataSanitization:
    def test_removes_blocked_keys(self) -> None:
        p = _make_enforcer()
        meta, warnings = p.sanitize_metadata({"api_key": "secret123", "safe_key": "ok"})
        assert "api_key" not in meta
        assert meta["safe_key"] == "ok"

    def test_none_metadata_passthrough(self) -> None:
        p = _make_enforcer()
        meta, warnings = p.sanitize_metadata(None)
        assert meta is None


class TestCircuitBreaker:
    def test_records_success_and_failure(self) -> None:
        p = _make_enforcer()
        p.check_circuit("test")  # Should not raise
        p.record_success()
        p.record_failure()

    def test_degraded_retain_returns_error(self) -> None:
        p = _make_enforcer()
        result = p.handle_degraded_retain("test")
        assert result.stored is False
        assert "unavailable" in (result.error or "").lower()

    def test_degraded_recall_returns_empty(self) -> None:
        p = _make_enforcer()
        result = p.handle_degraded_recall("test")
        assert result.hits == []


class TestPiiScanning:
    @pytest.mark.asyncio
    async def test_pii_disabled_passthrough(self) -> None:
        p = _make_enforcer(pii_mode="disabled")
        content, matches = await p.scan_pii("john@example.com", "disabled")
        assert content == "john@example.com"

    @pytest.mark.asyncio
    async def test_pii_regex_detects_email(self) -> None:
        p = _make_enforcer(pii_mode="regex")
        content, matches = await p.scan_pii("Email john@example.com here", "regex")
        assert len(matches) > 0
        assert any(m.pii_type == "email" for m in matches)
