"""Resolve caller identity for REST handlers (dev header vs API key vs JWT)."""

from __future__ import annotations

import hmac
import json
import logging
import os
from functools import lru_cache
from typing import Any

import jwt
from fastapi import Header, HTTPException
from jwt import PyJWKClient
from jwt.exceptions import PyJWTError

from astrocyte.types import ActorIdentity, AstrocyteContext

__all__ = ["get_astrocyte_context", "validate_auth_startup_config"]

_logger = logging.getLogger(__name__)

# Loopback hosts where dev-mode (no authentication) is considered safe.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def validate_auth_startup_config() -> None:
    """Fail closed on an unauthenticated deployment bound to a public address.

    ``dev`` auth mode performs no authentication — it trusts the client-supplied
    ``X-Astrocyte-Principal`` header verbatim. That is fine for local
    development on a loopback address, but a gateway bound to ``0.0.0.0`` (as
    the shipped Docker image and Compose file do) with the default ``dev`` mode
    is fully open: any caller can assert any principal and read any bank.

    This guard refuses to start such a deployment. Operators who genuinely want
    unauthenticated access on a public interface (e.g. behind a trusted mesh or
    an authenticating proxy) must opt in explicitly with
    ``ASTROCYTE_ALLOW_DEV_AUTH=1``.

    Raises:
        RuntimeError: if ``ASTROCYTE_AUTH_MODE`` is ``dev`` (or unset) AND
            ``ASTROCYTE_HOST`` is a non-loopback address AND the explicit
            opt-out is not set.
    """
    if _auth_mode() != "dev":
        return
    host = os.environ.get("ASTROCYTE_HOST", "127.0.0.1").strip()
    if host in _LOOPBACK_HOSTS:
        return
    if os.environ.get("ASTROCYTE_ALLOW_DEV_AUTH", "").strip().lower() in ("1", "true", "yes"):
        _logger.warning(
            "Running in dev auth mode (no authentication) on public host %s — "
            "ASTROCYTE_ALLOW_DEV_AUTH is set. The gateway trusts the "
            "X-Astrocyte-Principal header verbatim; ensure an upstream "
            "authenticating proxy or trusted network fronts this service.",
            host,
        )
        return
    raise RuntimeError(
        f"Refusing to start: auth mode is 'dev' (no authentication) but "
        f"ASTROCYTE_HOST={host!r} is not a loopback address. A dev-mode gateway "
        f"on a public interface is fully open — any caller can impersonate any "
        f"principal and read any bank. Set ASTROCYTE_AUTH_MODE to 'api_key', "
        f"'jwt_hs256', or 'jwt_oidc', bind to 127.0.0.1, or (if you truly intend "
        f"unauthenticated public access behind a trusted proxy) set "
        f"ASTROCYTE_ALLOW_DEV_AUTH=1."
    )


@lru_cache(maxsize=1)
def _warn_missing_jwt_audience() -> None:
    """Log a one-time warning when JWT audience validation is skipped."""
    _logger.warning(
        "ASTROCYTE_JWT_AUDIENCE is not set — tokens will be accepted without audience validation. "
        "Set this variable to prevent cross-service token reuse."
    )


def _auth_mode() -> str:
    return os.environ.get("ASTROCYTE_AUTH_MODE", "dev").strip().lower()


@lru_cache(maxsize=4)
def _jwks_client(jwks_url: str) -> PyJWKClient:
    """Return cached JWKS client for the given URL."""
    return PyJWKClient(jwks_url)


def _decode_oidc_rs256(token: str) -> dict[str, Any]:
    """Decode RS256 access token using JWKS (OIDC-style)."""
    jwks_url = os.environ.get("ASTROCYTE_OIDC_JWKS_URL", "").strip()
    if not jwks_url:
        raise HTTPException(status_code=500, detail="ASTROCYTE_OIDC_JWKS_URL is not set")
    issuer = os.environ.get("ASTROCYTE_OIDC_ISSUER", "").strip()
    audience = os.environ.get("ASTROCYTE_OIDC_AUDIENCE", "").strip()
    if not issuer or not audience:
        raise HTTPException(
            status_code=500,
            detail="ASTROCYTE_OIDC_ISSUER and ASTROCYTE_OIDC_AUDIENCE are required for jwt_oidc",
        )
    try:
        jwks_client = _jwks_client(jwks_url)
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
        )
    except PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"Invalid OIDC token: {e}") from e


def _context_from_oidc_payload(payload: dict[str, Any]) -> AstrocyteContext:
    sub = payload.get("sub")
    if not sub or not isinstance(sub, str):
        raise HTTPException(status_code=401, detail="Token missing sub")
    # Default actor type from environment; JWT claim takes precedence when present.
    default_actor_type = os.environ.get("ASTROCYTE_OIDC_ACTOR_TYPE", "user").strip().lower()
    claim_actor_type = payload.get("astrocyte_actor_type")
    actor_type = (
        claim_actor_type.strip().lower()
        if isinstance(claim_actor_type, str) and claim_actor_type.strip()
        else default_actor_type
    )
    tid = payload.get("tid") or payload.get("tenant_id")
    tenant_id = str(tid).strip() if tid is not None and str(tid).strip() else None
    # Standard JWT claims already consumed above or irrelevant to actor identity.
    _excluded_claims = {
        "sub", "iss", "aud", "exp", "nbf", "iat", "jti",
        "tid", "tenant_id", "astrocyte_actor_type", "astrocyte_principal",
    }
    claims: dict[str, str] = {}
    for k, v in payload.items():
        if k in _excluded_claims:
            continue
        if isinstance(v, (str, int, float, bool)):
            claims[k] = str(v)
        elif isinstance(v, (list, dict)):
            claims[k] = json.dumps(v)
        elif v is not None:
            _logger.debug("Dropping non-serializable claim %s of type %s", k, type(v).__name__)
    actor = ActorIdentity(type=actor_type, id=sub, claims=claims or None)
    principal = str(payload.get("astrocyte_principal") or f"{actor_type}:{sub}")
    return AstrocyteContext(principal=principal, actor=actor, tenant_id=tenant_id)


def resolve_astrocyte_identity(
    *,
    authorization: str | None,
    x_api_key: str | None,
    x_astrocyte_principal: str | None,
) -> AstrocyteContext | None:
    """Return authenticated context, or None when anonymous is allowed."""
    mode = _auth_mode()
    if mode == "dev":
        if x_astrocyte_principal is None or x_astrocyte_principal == "":
            return None
        return AstrocyteContext(principal=x_astrocyte_principal)

    if mode == "api_key":
        expected = os.environ.get("ASTROCYTE_API_KEY", "")
        if not expected:
            raise HTTPException(status_code=500, detail="ASTROCYTE_API_KEY is not set")
        if not hmac.compare_digest(x_api_key or "", expected):
            raise HTTPException(status_code=401, detail="Invalid or missing API key")
        if not x_astrocyte_principal:
            raise HTTPException(
                status_code=401,
                detail="X-Astrocyte-Principal required when using API key auth",
            )
        return AstrocyteContext(principal=x_astrocyte_principal)

    if mode in ("jwt_hs256", "jwt"):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Bearer token required")
        token = authorization.removeprefix("Bearer ").strip()
        secret = os.environ.get("ASTROCYTE_JWT_SECRET", "")
        if not secret:
            raise HTTPException(status_code=500, detail="ASTROCYTE_JWT_SECRET is not set")
        aud = os.environ.get("ASTROCYTE_JWT_AUDIENCE")
        decode_kwargs: dict = {"algorithms": ["HS256"]}
        if aud:
            decode_kwargs["audience"] = aud
        else:
            _warn_missing_jwt_audience()
        try:
            payload = jwt.decode(token, secret, **decode_kwargs)
        except PyJWTError as e:
            raise HTTPException(status_code=401, detail="Invalid token") from e
        sub = payload.get("sub")
        if not sub or not isinstance(sub, str):
            raise HTTPException(status_code=401, detail="Token missing sub")
        return AstrocyteContext(principal=sub)

    if mode == "jwt_oidc":
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Bearer token required")
        token = authorization.removeprefix("Bearer ").strip()
        payload = _decode_oidc_rs256(token)
        return _context_from_oidc_payload(payload)

    raise HTTPException(status_code=500, detail=f"Unknown ASTROCYTE_AUTH_MODE: {mode}")


async def get_astrocyte_context(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
    x_astrocyte_principal: str | None = Header(default=None, alias="X-Astrocyte-Principal"),
) -> AstrocyteContext | None:
    """FastAPI dependency: trusted principal from JWT/API key, or dev header."""
    return resolve_astrocyte_identity(
        authorization=authorization,
        x_api_key=x_api_key,
        x_astrocyte_principal=x_astrocyte_principal,
    )
