"""Resolve caller identity for REST handlers (dev header vs API key vs JWT)."""

from __future__ import annotations

import hmac
import os
from typing import Any

import jwt
from fastapi import Header, HTTPException
from jwt import PyJWKClient
from jwt.exceptions import PyJWTError

from astrocyte.types import ActorIdentity, AstrocyteContext

__all__ = ["get_astrocyte_context"]


def _auth_mode() -> str:
    return os.environ.get("ASTROCYTE_AUTH_MODE", "dev").strip().lower()


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
        jwks_client = PyJWKClient(jwks_url)
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
    actor_type = os.environ.get("ASTROCYTE_OIDC_ACTOR_TYPE", "user").strip().lower()
    claim_type = payload.get("astrocyte_actor_type")
    if isinstance(claim_type, str) and claim_type.strip():
        actor_type = claim_type.strip().lower()
    tid = payload.get("tid") or payload.get("tenant_id")
    tenant_id = str(tid).strip() if tid is not None and str(tid).strip() else None
    claims = {k: str(v) for k, v in payload.items() if isinstance(v, (str, int, float, bool))}
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
        if not expected or not hmac.compare_digest(x_api_key or "", expected):
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
