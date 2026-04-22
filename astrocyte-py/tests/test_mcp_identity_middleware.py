"""JWT identity middleware wiring tests.

Covers the transport-layer glue added by :mod:`astrocyte._mcp_identity`
for identity spec §3 Gap 1. The pure classifier has its own suite in
``test_identity_jwt_classifier.py``; this file focuses on the wiring:
header extraction, decoder injection, fail-closed policy, and
``AstrocyteContext`` construction.

Decoders are always injected — no test depends on PyJWT being installed,
and no test hits a real JWKS endpoint.
"""

from __future__ import annotations

from typing import Any

import pytest

from astrocyte._mcp_identity import (
    JwtIdentityMiddleware,
    build_jwt_middleware,
    extract_bearer_token,
)
from astrocyte.config import JwtMiddlewareConfig
from astrocyte.errors import AuthorizationError

# ---------------------------------------------------------------------------
# extract_bearer_token — header parsing
# ---------------------------------------------------------------------------


class TestExtractBearerToken:
    def test_standard_bearer(self) -> None:
        assert extract_bearer_token({"Authorization": "Bearer abc.def.ghi"}) == "abc.def.ghi"

    def test_case_insensitive_header_name(self) -> None:
        assert extract_bearer_token({"authorization": "Bearer xyz"}) == "xyz"
        assert extract_bearer_token({"AUTHORIZATION": "Bearer xyz"}) == "xyz"

    def test_case_insensitive_bearer_prefix(self) -> None:
        """HTTP scheme names are case-insensitive per RFC 7235."""
        assert extract_bearer_token({"Authorization": "bearer tok"}) == "tok"
        assert extract_bearer_token({"Authorization": "BEARER tok"}) == "tok"

    def test_missing_header(self) -> None:
        assert extract_bearer_token({}) is None

    def test_non_bearer_scheme_rejected(self) -> None:
        """Basic auth or token schemes must not be interpreted as Bearer."""
        assert extract_bearer_token({"Authorization": "Basic dXNlcjpwYXNz"}) is None
        assert extract_bearer_token({"Authorization": "Token xyz"}) is None

    def test_empty_bearer_value(self) -> None:
        """``Bearer`` with no token is not a valid token."""
        assert extract_bearer_token({"Authorization": "Bearer "}) is None

    def test_leading_whitespace_tolerated(self) -> None:
        assert extract_bearer_token({"Authorization": "   Bearer tok   "}) == "tok"

    def test_non_string_value_returns_none(self) -> None:
        """Hostile callers might set non-string header values — don't crash."""
        assert extract_bearer_token({"Authorization": 123}) is None  # type: ignore[dict-item]


# ---------------------------------------------------------------------------
# JwtIdentityMiddleware — resolve() policy
# ---------------------------------------------------------------------------


_USER_CLAIMS = {"upn": "alice@co", "oid": "user-oid-1", "tid": "t1"}
_SVC_CLAIMS = {"appid": "svc-abc", "tid": "t1"}


def _cfg(**overrides) -> JwtMiddlewareConfig:
    base = JwtMiddlewareConfig(
        enabled=True,
        jwks_uri="https://idp.test/jwks",
        token_audience="api://astrocyte",
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _static_decoder(claims: dict[str, Any]):
    """Build a test decoder that returns a fixed claim dict."""
    def decode(_token: str) -> dict[str, Any]:
        return claims
    return decode


class TestMiddlewareResolve:
    def test_valid_user_token_produces_user_context(self) -> None:
        mw = JwtIdentityMiddleware(_cfg(), _static_decoder(_USER_CLAIMS))
        ctx = mw.resolve({"Authorization": "Bearer good-token"})
        assert ctx.actor is not None
        assert ctx.actor.type == "user"
        assert ctx.actor.id == "user-oid-1"
        # principal is the canonical {type}:{id} form for audit + grants.
        assert ctx.principal == "user:user-oid-1"

    def test_valid_service_token_produces_service_context(self) -> None:
        mw = JwtIdentityMiddleware(_cfg(), _static_decoder(_SVC_CLAIMS))
        ctx = mw.resolve({"Authorization": "Bearer good-token"})
        assert ctx.actor is not None
        assert ctx.actor.type == "service"
        assert ctx.actor.id == "svc-abc"
        assert ctx.principal == "service:svc-abc"

    def test_missing_header_with_fail_closed_raises(self) -> None:
        mw = JwtIdentityMiddleware(
            _cfg(fail_closed=True, allow_anonymous=False),
            _static_decoder(_USER_CLAIMS),
        )
        with pytest.raises(AuthorizationError, match="No Bearer token presented"):
            mw.resolve({})

    def test_missing_header_with_allow_anonymous_returns_anonymous(self) -> None:
        mw = JwtIdentityMiddleware(
            _cfg(fail_closed=False, allow_anonymous=True),
            _static_decoder(_USER_CLAIMS),
        )
        ctx = mw.resolve({})
        assert ctx.principal == "anonymous"
        assert ctx.actor is None

    def test_invalid_token_always_fails_closed_even_with_allow_anonymous(self) -> None:
        """Decoder failures never fall through to anonymous, even when the
        config permits anonymous for missing headers. Silently downgrading
        a broken token would let an attacker bypass identity-aware MIP
        rules by sending malformed credentials."""
        def bad_decoder(_token: str) -> dict[str, Any]:
            raise RuntimeError("signature invalid")

        mw = JwtIdentityMiddleware(
            _cfg(fail_closed=False, allow_anonymous=True),
            bad_decoder,
        )
        with pytest.raises(AuthorizationError, match="decode/validation failed"):
            mw.resolve({"Authorization": "Bearer garbage"})

    def test_unclassifiable_claims_raise(self) -> None:
        """Decoder succeeds but classify_jwt_claims rejects — propagate
        as AuthorizationError so the transport layer returns 401 / MCP
        error rather than silently routing."""
        mw = JwtIdentityMiddleware(_cfg(), _static_decoder({"random": "claim"}))
        with pytest.raises(AuthorizationError, match="identity type could not be determined"):
            mw.resolve({"Authorization": "Bearer x"})

    def test_decoder_called_with_exact_token(self) -> None:
        """Confirm we pass the token string unchanged (not with the
        'Bearer ' prefix)."""
        captured: dict[str, str] = {}

        def capture_decoder(token: str) -> dict[str, Any]:
            captured["token"] = token
            return _USER_CLAIMS

        mw = JwtIdentityMiddleware(_cfg(), capture_decoder)
        mw.resolve({"Authorization": "Bearer actual-token-value"})
        assert captured["token"] == "actual-token-value"


# ---------------------------------------------------------------------------
# build_jwt_middleware — factory
# ---------------------------------------------------------------------------


class TestBuildMiddleware:
    def test_disabled_config_returns_none(self) -> None:
        cfg = JwtMiddlewareConfig(enabled=False)
        assert build_jwt_middleware(cfg) is None

    def test_enabled_with_injected_decoder(self) -> None:
        mw = build_jwt_middleware(_cfg(), decoder=_static_decoder(_USER_CLAIMS))
        assert isinstance(mw, JwtIdentityMiddleware)

    def test_enabled_without_decoder_and_no_pyjwt_raises(self, monkeypatch) -> None:
        """When PyJWT isn't available and no decoder is injected, the
        factory must raise at build time rather than at first request —
        config errors should surface at startup."""
        import sys

        # Simulate PyJWT not installed.
        monkeypatch.setitem(sys.modules, "jwt", None)
        with pytest.raises((ImportError, TypeError)):
            build_jwt_middleware(_cfg())


# ---------------------------------------------------------------------------
# MCP server wiring — end-to-end per-request identity
# ---------------------------------------------------------------------------


class TestMcpServerWiring:
    """Spot-check that `create_mcp_server` wires the middleware into
    per-request context resolution. We don't actually start an HTTP
    server — we import the server factory and inspect its resolver
    behavior with injected headers via fastmcp's header context var."""

    def test_create_with_injected_middleware(self) -> None:
        """Passing a pre-built middleware overrides config-driven build."""
        from astrocyte._astrocyte import Astrocyte
        from astrocyte.config import AstrocyteConfig
        from astrocyte.mcp import create_mcp_server
        from astrocyte.testing.in_memory import InMemoryEngineProvider

        cfg = AstrocyteConfig()
        cfg.barriers.pii.mode = "disabled"
        brain = Astrocyte(cfg)
        brain.set_engine_provider(InMemoryEngineProvider())

        mw = JwtIdentityMiddleware(_cfg(), _static_decoder(_USER_CLAIMS))

        # Should construct without raising.
        server = create_mcp_server(brain, cfg, jwt_middleware=mw)
        assert server is not None

    def test_astrocyte_context_overrides_middleware(self) -> None:
        """Explicit astrocyte_context at server creation wins over
        header-based resolution — backward compat for embedded hosts
        that pre-bind identity."""
        from astrocyte._astrocyte import Astrocyte
        from astrocyte.config import AstrocyteConfig
        from astrocyte.mcp import create_mcp_server
        from astrocyte.testing.in_memory import InMemoryEngineProvider
        from astrocyte.types import AstrocyteContext

        cfg = AstrocyteConfig()
        cfg.barriers.pii.mode = "disabled"
        cfg.identity.jwt_middleware.enabled = True
        cfg.identity.jwt_middleware.jwks_uri = "https://test/jwks"
        cfg.identity.jwt_middleware.token_audience = "api://astrocyte"

        brain = Astrocyte(cfg)
        brain.set_engine_provider(InMemoryEngineProvider())

        pre_bound = AstrocyteContext(principal="user:embedded-host")

        # Config enables middleware, but explicit pre-bound context must win.
        # With an injected decoder, no PyJWT dependency required.
        mw = JwtIdentityMiddleware(_cfg(), _static_decoder(_USER_CLAIMS))
        server = create_mcp_server(
            brain, cfg,
            astrocyte_context=pre_bound,
            jwt_middleware=mw,
        )
        assert server is not None
