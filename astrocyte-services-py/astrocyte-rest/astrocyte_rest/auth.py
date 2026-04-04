"""Resolve caller identity for REST handlers (dev header vs API key vs JWT)."""

from __future__ import annotations

import os

import jwt
from fastapi import Header, HTTPException

from astrocyte.types import AstrocyteContext


def _auth_mode() -> str:
    return os.environ.get("ASTROCYTES_AUTH_MODE", "dev").strip().lower()


def resolve_principal(
    *,
    authorization: str | None,
    x_api_key: str | None,
    x_astrocyte_principal: str | None,
) -> str | None:
    """Return authenticated principal id, or None for anonymous (when policy allows)."""
    mode = _auth_mode()
    if mode == "dev":
        if x_astrocyte_principal is None or x_astrocyte_principal == "":
            return None
        return x_astrocyte_principal

    if mode == "api_key":
        expected = os.environ.get("ASTROCYTES_API_KEY", "")
        if not expected or x_api_key != expected:
            raise HTTPException(status_code=401, detail="Invalid or missing API key")
        if not x_astrocyte_principal:
            raise HTTPException(
                status_code=401,
                detail="X-Astrocytes-Principal required when using API key auth",
            )
        return x_astrocyte_principal

    if mode in ("jwt_hs256", "jwt"):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Bearer token required")
        token = authorization.removeprefix("Bearer ").strip()
        secret = os.environ.get("ASTROCYTES_JWT_SECRET", "")
        if not secret:
            raise HTTPException(status_code=500, detail="ASTROCYTES_JWT_SECRET is not set")
        aud = os.environ.get("ASTROCYTES_JWT_AUDIENCE")
        decode_kwargs: dict = {"algorithms": ["HS256"]}
        if aud:
            decode_kwargs["audience"] = aud
        try:
            payload = jwt.decode(token, secret, **decode_kwargs)
        except jwt.PyJWTError as e:
            raise HTTPException(status_code=401, detail="Invalid token") from e
        sub = payload.get("sub")
        if not sub or not isinstance(sub, str):
            raise HTTPException(status_code=401, detail="Token missing sub")
        return sub

    raise HTTPException(status_code=500, detail=f"Unknown ASTROCYTES_AUTH_MODE: {mode}")


async def get_astrocyte_context(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
    x_astrocyte_principal: str | None = Header(default=None, alias="X-Astrocytes-Principal"),
) -> AstrocyteContext | None:
    """FastAPI dependency: trusted principal from JWT/API key, or dev header."""
    principal = resolve_principal(
        authorization=authorization,
        x_api_key=x_api_key,
        x_astrocyte_principal=x_astrocyte_principal,
    )
    if principal is None or principal == "":
        return None
    return AstrocyteContext(principal=principal)
