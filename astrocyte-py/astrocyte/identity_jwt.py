"""JWT claim classification → :class:`ActorIdentity`.

This module is intentionally pure: no network I/O, no dependency on
``PyJWT`` or any specific token-signing library. It takes a decoded claim
dict and returns an :class:`ActorIdentity`. The MCP transport layer is
responsible for extracting, decoding, and signature-validating the token
before handing the claim dict here; see
``docs/_plugins/jwt-identity-middleware.md``.

The split exists because:

1. Classification rules (which claim signals a user token vs a service
   account credential) are stable and testable offline.
2. Token decoding and JWKS handling are deployment-specific (IdP, algorithm
   suite, audience, clock skew) and belong in the MCP wiring layer.
3. Keeping the core pure means the whole ruleset can be exercised by unit
   tests with fake claim dicts — no test fixtures, no expired-token
   machinery, no JWKS mocks.

References:

- ``docs/_design/astrocyte_identity_spec.md`` §3 Gap 1 (the full spec that
  this implements, modulo the MCP wiring which lives in ``astrocyte.mcp``).
- ``docs/_design/adr/adr-002-identity-model.md`` (why :class:`ActorIdentity`
  already uses ``type`` ∈ {user, agent, service} and how JWT classification
  fits into it).
"""

from __future__ import annotations

from typing import Any

from astrocyte.errors import AuthorizationError
from astrocyte.types import ActorIdentity

# ---------------------------------------------------------------------------
# Claim name constants
# ---------------------------------------------------------------------------
# Names come from the OpenID Connect spec, OAuth 2.0 JWT profile, and the two
# largest IdP dialects (Entra ID / Azure AD and Google). Keeping them as
# constants lets deployments override via a future config without touching
# classification logic.

#: Claims that prove a delegated user token (agent acting on behalf of a human).
#: The first that resolves to a non-empty string is taken as the user's display
#: identifier (email / UPN). This is used for audit logs, never as a bank key.
_USER_DISPLAY_CLAIMS: tuple[str, ...] = ("upn", "preferred_username", "email")

#: Claims that carry the user's immutable subject identifier. The first that
#: resolves wins. ``oid`` is Entra-specific; ``sub`` is the OIDC standard.
#: Whichever is used becomes :attr:`ActorIdentity.id` and therefore the bank
#: key — picking a stable identifier is load-bearing (see spec §3 Gap 1
#: "Why OID / sub — not email or username — as the bank key").
_USER_SUBJECT_CLAIMS: tuple[str, ...] = ("oid", "sub")

#: Claims that carry the service account's application ID. First wins.
#: ``appid`` and ``azp`` are Entra / OIDC conventions; ``client_id`` appears
#: in some IdPs. Without one of these we cannot key a service bank.
_SERVICE_APP_CLAIMS: tuple[str, ...] = ("appid", "azp", "client_id")

#: Claim that explicitly declares token type in Entra ID v2. When set to
#: ``"app"`` the token is a service account credential regardless of any
#: user-looking claims that may also be present.
_IDTYP_APP_SIGNAL: str = "app"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_jwt_claims(
    claims: dict[str, Any],
) -> ActorIdentity:
    """Classify a decoded JWT claim dict into an :class:`ActorIdentity`.

    The classification rules mirror the spec's §3 Gap 1 decision tree:

    1. **Service account override**: ``idtyp == "app"`` is an explicit
       Entra ID v2 signal — respected even if user-looking claims are
       present, because a service account can legitimately carry a ``sub``.
    2. **Delegated user token**: any of :data:`_USER_DISPLAY_CLAIMS` is
       present AND ``idtyp != "app"``. The subject identifier comes from
       the first resolving :data:`_USER_SUBJECT_CLAIMS` — never from
       ``email``/``upn`` (those mutate; subject identifiers are stable).
    3. **Service account (fallback)**: any of :data:`_SERVICE_APP_CLAIMS`
       is present.
    4. **Unclassifiable**: raise :class:`AuthorizationError`. Fail closed
       — we never mint an anonymous identity for a token we successfully
       decoded but cannot classify.

    The returned :class:`ActorIdentity` stashes the display identifier,
    app id, tenant id, and ``idtyp`` into ``claims`` for audit trails;
    the raw claim bag is not persisted.

    Args:
        claims: A decoded and signature-validated claim dict. Caller is
            responsible for signature verification, expiry, audience, and
            issuer — those are deployment-specific.

    Returns:
        :class:`ActorIdentity` whose ``type`` is ``"user"`` or ``"service"``
        and whose ``id`` is the stable identifier for that principal. Use
        :func:`derive_bank_id` (below) to turn it into a bank id.

    Raises:
        AuthorizationError: If the claim dict cannot be classified. Never
            returns a fallback identity — silent fallthrough would route
            authenticated data into a shared default bank.
    """
    if not isinstance(claims, dict):
        raise AuthorizationError(
            "Token claims must be a dict (got "
            f"{type(claims).__name__}). Decoded token is malformed."
        )

    tenant_id = _nonempty_str(claims.get("tid"))
    idtyp = _nonempty_str(claims.get("idtyp"))

    # 1. Service account override via explicit idtyp signal.
    if idtyp == _IDTYP_APP_SIGNAL:
        return _build_service_identity(claims, tenant_id, idtyp)

    # 2. Delegated user token — user display claim present and idtyp != app.
    user_display = _first_nonempty(claims, _USER_DISPLAY_CLAIMS)
    if user_display is not None:
        subject = _first_nonempty(claims, _USER_SUBJECT_CLAIMS)
        if subject is None:
            raise AuthorizationError(
                "Delegated user token has a display claim "
                f"({user_display!r}) but no stable subject claim "
                f"({list(_USER_SUBJECT_CLAIMS)}). Refusing to route — bank "
                "key must be stable across email/UPN changes."
            )
        stashed = {"upn": user_display}
        if idtyp is not None:
            stashed["idtyp"] = idtyp
        return ActorIdentity(
            type="user",
            id=subject,
            claims=_with_tenant(stashed, tenant_id),
        )

    # 3. Service account fallback — app id claim present.
    if _first_nonempty(claims, _SERVICE_APP_CLAIMS) is not None:
        return _build_service_identity(claims, tenant_id, idtyp)

    # 4. Unclassifiable — fail closed.
    raise AuthorizationError(
        "Token decoded but identity type could not be determined. "
        "Expected a delegated user token (one of "
        f"{list(_USER_DISPLAY_CLAIMS)}) or a service account credential "
        f"(one of {list(_SERVICE_APP_CLAIMS)}, or idtyp=app). "
        "Available claim keys: "
        f"{sorted(claims.keys()) if claims else '[]'}."
    )


def derive_bank_id(
    identity: ActorIdentity,
    *,
    service_bank_prefix: str = "service-",
    user_bank_prefix: str = "user-",
) -> str:
    """Bank id for a classified identity, using the caller's prefix choice.

    Kept separate from :func:`classify_jwt_claims` because the bank-prefix
    convention is a deployment decision (``svc-`` vs ``service-``) and
    should not leak into the identity object itself — the identity is
    stable, bank naming is a policy.
    """
    if identity.type == "user":
        return f"{user_bank_prefix}{identity.id}"
    if identity.type == "service":
        return f"{service_bank_prefix}{identity.id}"
    raise ValueError(
        f"Cannot derive bank id for identity.type={identity.type!r}; "
        "only 'user' and 'service' identities are supported by the JWT "
        "classifier."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_service_identity(
    claims: dict[str, Any],
    tenant_id: str | None,
    idtyp: str | None,
) -> ActorIdentity:
    app_id = _first_nonempty(claims, _SERVICE_APP_CLAIMS)
    if app_id is None:
        raise AuthorizationError(
            "Token classified as service account (idtyp=app) but no app id "
            f"claim found (expected one of {list(_SERVICE_APP_CLAIMS)})."
        )
    stashed = {"app_id": app_id}
    if idtyp is not None:
        stashed["idtyp"] = idtyp
    return ActorIdentity(
        type="service",
        id=app_id,
        claims=_with_tenant(stashed, tenant_id),
    )


def _with_tenant(base: dict[str, str], tenant_id: str | None) -> dict[str, str]:
    if tenant_id is not None:
        base = {**base, "tenant_id": tenant_id}
    return base


def _first_nonempty(claims: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = _nonempty_str(claims.get(key))
        if value is not None:
            return value
    return None


def _nonempty_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None
