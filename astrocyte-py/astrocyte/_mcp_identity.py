"""JWT identity middleware wiring for the Astrocyte MCP server.

This module is the **transport layer** counterpart to the pure classifier
in :mod:`astrocyte.identity_jwt`. It accepts a Bearer token from an
incoming request, validates it, extracts the claims, and builds an
:class:`AstrocyteContext` for the request.

The decoder is **injectable** so the wiring can be tested without PyJWT
as a runtime dependency (unit tests feed pre-decoded claim dicts); the
default decoder uses PyJWT + a JWKS client when available.

See:

- :mod:`astrocyte.identity_jwt` — the pure claim-to-identity classifier
- ``docs/_plugins/jwt-identity-middleware.md`` — operator guide
- ``docs/_design/astrocyte_identity_spec.md`` §3 Gap 1 — architectural spec
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from astrocyte.config import JwtMiddlewareConfig
from astrocyte.errors import AuthorizationError
from astrocyte.identity import format_principal
from astrocyte.identity_jwt import classify_jwt_claims
from astrocyte.types import AstrocyteContext

logger = logging.getLogger("astrocyte.identity.mcp")

#: Type alias for the decoder strategy. Given a raw JWT string, returns a
#: decoded and signature-validated claim dict. Raises on any failure —
#: the middleware maps exceptions to :class:`AuthorizationError`.
Decoder = Callable[[str], dict[str, Any]]


# ---------------------------------------------------------------------------
# Header extraction
# ---------------------------------------------------------------------------


def extract_bearer_token(headers: dict[str, str]) -> str | None:
    """Pull the Bearer token from an ``Authorization`` header, if any.

    Header lookup is case-insensitive. Returns None when the header is
    absent or doesn't start with ``Bearer ``; the middleware decides how
    to handle that (fail closed vs anonymous fallback).
    """
    if not headers:
        return None
    # Case-insensitive lookup — HTTP header names aren't case-sensitive.
    for key, value in headers.items():
        if key.lower() == "authorization":
            if not isinstance(value, str):
                return None
            stripped = value.strip()
            if stripped.lower().startswith("bearer "):
                # Slice past "Bearer " (7 chars) preserving the token.
                return stripped[7:].strip() or None
            return None
    return None


# ---------------------------------------------------------------------------
# Middleware core — transport-independent
# ---------------------------------------------------------------------------


class JwtIdentityMiddleware:
    """Resolve an :class:`AstrocyteContext` from inbound request headers.

    The middleware orchestrates four steps:

    1. Extract the Bearer token from the Authorization header.
    2. If no token present, apply the anonymous / fail-closed policy.
    3. Decode + validate the token via the injected :data:`Decoder`.
    4. Classify the claims via :func:`classify_jwt_claims` and build an
       :class:`AstrocyteContext` whose ``actor`` is the resolved identity
       and whose ``principal`` is the canonical ``{type}:{id}`` form.

    The decoder is injected so tests (and embedded deployments with
    custom token formats) can replace PyJWT with a minimal substitute.
    Production deployments use :func:`make_pyjwt_decoder` which wires up
    a JWKS client against the configured endpoint.
    """

    def __init__(
        self,
        config: JwtMiddlewareConfig,
        decoder: Decoder,
    ) -> None:
        self._config = config
        self._decoder = decoder

    def resolve(self, headers: dict[str, str]) -> AstrocyteContext:
        """Resolve caller identity from inbound headers.

        Returns a fully-formed :class:`AstrocyteContext` — ``principal``
        is always populated so downstream access control and audit logs
        have a label even for anonymous callers.

        Raises:
            AuthorizationError: Bearer token was presented but invalid
                (signature, expiry, audience, issuer, or classification
                failure); or no token was presented but anonymous access
                is disabled; or the middleware is enabled but the caller
                has no ``Authorization`` header.
        """
        token = extract_bearer_token(headers)

        if token is None:
            # No credential presented. Policy decides whether that's OK.
            if self._config.fail_closed and not self._config.allow_anonymous:
                raise AuthorizationError(
                    "No Bearer token presented and anonymous access is "
                    "disabled (identity.jwt_middleware.allow_anonymous=false)."
                )
            # Anonymous fallback — principal label makes this visible in logs.
            return AstrocyteContext(principal="anonymous")

        # Token presented — validate and classify. Any failure here is
        # fail-closed regardless of allow_anonymous: a broken token is
        # never silently downgraded to anonymous (that would let an
        # attacker bypass identity-aware MIP rules by malforming their
        # token).
        try:
            claims = self._decoder(token)
        except AuthorizationError:
            raise
        except Exception as exc:  # pragma: no cover — decoder-specific
            logger.warning("JWT decode failed: %s", exc)
            raise AuthorizationError(
                f"Bearer token decode/validation failed: {exc}. "
                "Request rejected to prevent cross-user data leakage."
            ) from exc

        identity = classify_jwt_claims(claims)
        return AstrocyteContext(
            principal=format_principal(identity),
            actor=identity,
        )


# ---------------------------------------------------------------------------
# Default PyJWT-backed decoder (optional)
# ---------------------------------------------------------------------------


class _JWKSClient(Protocol):
    def get_signing_key_from_jwt(self, token: str) -> Any:  # pragma: no cover
        """Return the signing key for ``token`` (PyJWT-compatible shape)."""


@dataclass
class _PyJWTContext:
    """Captured state for the default PyJWT decoder."""

    jwks_client: _JWKSClient
    audience: str | None
    issuer: str | None
    algorithms: list[str]


def make_pyjwt_decoder(config: JwtMiddlewareConfig) -> Decoder:
    """Build a PyJWT-backed :data:`Decoder` from middleware config.

    Raises :class:`ImportError` if PyJWT isn't installed. This keeps
    PyJWT an optional dependency — deployments that don't enable JWT
    middleware don't pay for it.

    The decoder enforces:
    - Signature via JWKS-resolved public key
    - Algorithm allowlist (defaults to asymmetric — RS256, ES256)
    - ``aud`` claim match (required — set on config)
    - ``iss`` claim match (when configured)
    - ``exp``, ``iat`` required claims — rejects tokens missing them
    """
    try:
        import jwt  # PyJWT  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover — optional dep
        raise ImportError(
            "JWT identity middleware requires PyJWT. Install with "
            "'pip install pyjwt[cryptography]' or enable the 'identity' "
            "extra on the astrocyte package."
        ) from exc

    if not config.jwks_uri:
        raise ValueError(
            "identity.jwt_middleware.jwks_uri must be set when enabled."
        )
    if not config.token_audience:
        raise ValueError(
            "identity.jwt_middleware.token_audience must be set when "
            "enabled — accepting any audience risks cross-tenant token "
            "reuse."
        )

    jwks_client = jwt.PyJWKClient(
        config.jwks_uri,
        cache_keys=True,
        lifespan=config.jwks_refresh_interval_hours * 3600,
    )
    ctx = _PyJWTContext(
        jwks_client=jwks_client,
        audience=config.token_audience,
        issuer=config.token_issuer,
        algorithms=list(config.algorithms),
    )

    def decode(token: str) -> dict[str, Any]:
        signing_key = ctx.jwks_client.get_signing_key_from_jwt(token)
        options: dict[str, Any] = {"require": ["exp", "iat"]}
        kwargs: dict[str, Any] = {
            "algorithms": ctx.algorithms,
            "options": options,
        }
        if ctx.audience is not None:
            kwargs["audience"] = ctx.audience
        if ctx.issuer is not None:
            kwargs["issuer"] = ctx.issuer
        return jwt.decode(token, signing_key.key, **kwargs)

    return decode


# ---------------------------------------------------------------------------
# Top-level factory
# ---------------------------------------------------------------------------


def build_jwt_middleware(
    config: JwtMiddlewareConfig,
    decoder: Decoder | None = None,
) -> JwtIdentityMiddleware | None:
    """Build a middleware if enabled in config, else return None.

    Args:
        config: The ``identity.jwt_middleware`` section of AstrocyteConfig.
        decoder: Optional decoder override. When None (default), a
            PyJWT-backed decoder is constructed from the config; pass a
            custom callable for testing or non-PyJWT deployments.
    """
    if not config.enabled:
        return None
    if decoder is None:
        decoder = make_pyjwt_decoder(config)
    return JwtIdentityMiddleware(config, decoder)
