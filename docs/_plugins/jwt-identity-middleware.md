---
title: JWT Identity Middleware
description: Resolve per-user bank identity from delegated tokens and service account credentials at the MCP boundary.
---

# JWT Identity Middleware

This guide is the operator / integrator reference for the identity layer
defined in [Astrocyte Identity Spec](../_design/astrocyte_identity_spec.md).
Read the spec first for the architectural framing; this guide is the
"how do I wire this up" companion.

## What it does

On every inbound MCP request, a middleware layer:

1. Reads the `Authorization: Bearer <token>` header.
2. Decodes and signature-validates the JWT against the IdP's JWKS.
3. Classifies the token as a **delegated user token** or a **service
   account credential** using [`astrocyte.identity_jwt.classify_jwt_claims`](#classify_jwt_claims).
4. Populates `AstrocyteContext.actor` with an `ActorIdentity` whose
   `type` is `"user"` or `"service"` and whose `id` is the immutable
   subject identifier (`oid` / `sub` for users, `appid` for service
   accounts).
5. Lets the rest of the pipeline derive the `bank_id`, enforce access
   control, and evaluate MIP rules against the resolved principal.

Callers can still pass an explicit `astrocyte_context` — the middleware
only fills in identity when the host has not already done so.

## Why split classification from transport

The `astrocyte.identity_jwt` module contains the **pure** classification
logic: given a decoded claim dict, it returns an `ActorIdentity` or
raises `AuthorizationError`. The MCP transport layer is responsible for
the token work that is deployment-specific:

- Signature verification and JWKS fetching (different IdPs, different
  key-rotation cadences)
- Audience / issuer / expiry / clock-skew validation
- HTTP error mapping (401 vs 403 vs 500)

This split is intentional. It means:

- The classification ruleset is fully testable offline — no expired-token
  fixtures, no JWKS mocks.
- Deployments with unusual IdPs (internal SSO, custom signing) can reuse
  the classifier while supplying their own token decoder.
- Rust / other-language ports of Astrocyte can reimplement the classifier
  without also reimplementing JWT validation.

## `classify_jwt_claims` reference

```python
from astrocyte.identity_jwt import classify_jwt_claims, derive_bank_id

identity = classify_jwt_claims(decoded_claims)
# ActorIdentity(type="user" | "service", id=<stable-identifier>, claims={...})

bank_id = derive_bank_id(identity)
# "user-<oid>" or "service-<app_id>", or pass service_bank_prefix="svc-"
```

### Claim signals the classifier looks for

**Delegated user token** — any of these, AND `idtyp != "app"`:

| Signal | Source | Purpose |
|---|---|---|
| `upn` | Entra ID / AAD | Display identifier (email-shaped). Stashed in `identity.claims["upn"]` for audit. |
| `preferred_username` | OIDC standard | Same. |
| `email` | Some IdPs | Same. |

The stable identifier comes from the first resolving of:
| Claim | Source | Why chosen |
|---|---|---|
| `oid` | Entra ID | Per-user immutable ID (unlike `sub`, which is per app-pair). |
| `sub` | OIDC standard | Standard subject identifier. |

**Service account credential** — any of:

| Signal | Source | Purpose |
|---|---|---|
| `idtyp == "app"` | Entra ID v2 | **Explicit override** — even if user-looking claims present. |
| `appid` | Entra ID | Application ID. |
| `azp` | OIDC | Authorized party. |
| `client_id` | Some IdPs | Client ID. |

The `id` field of the resulting `ActorIdentity` is the first resolving
of `appid` / `azp` / `client_id`.

### Fail-closed behavior

Every unexpected shape raises `AuthorizationError`:

- Empty claim dict
- Display claim present but no stable subject
- `idtyp=app` but no `appid` / `azp` / `client_id`
- Non-string or whitespace-only claim values
- Unknown claim shape altogether

There is **no** fallback to anonymous. A credential that was presented
and could not be classified must never silently route to a default bank
— that is the cross-user leakage mode this whole layer exists to
prevent.

If you need anonymous access, the middleware must fall through to
anonymous **before** reaching `classify_jwt_claims` — typically by
checking that no `Authorization` header was presented at all.

## Wiring the classifier into your MCP host

The `astrocyte-mcp` command-line server does not yet enable the middleware
by default — the wiring depends on your IdP and HTTP framework. The
minimal pattern:

```python
from astrocyte.errors import AuthorizationError
from astrocyte.identity_jwt import classify_jwt_claims, derive_bank_id
from astrocyte.types import AstrocyteContext

def build_astrocyte_context(request, *, jwks_client, audience):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        if not allow_anonymous:
            raise AuthorizationError(
                "No Bearer token presented and anonymous access is disabled."
            )
        return AstrocyteContext(principal="anonymous")

    token = auth.removeprefix("Bearer ")
    signing_key = jwks_client.get_signing_key_from_jwt(token)
    claims = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256", "ES256"],
        audience=audience,
        options={"require": ["exp", "iat", "sub"]},
    )

    identity = classify_jwt_claims(claims)
    return AstrocyteContext(
        principal=f"{identity.type}:{identity.id}",
        actor=identity,
    )
```

Pass the resulting `AstrocyteContext` to `brain.retain(..., context=ctx)`
and `brain.recall(..., context=ctx)` — the rest of the pipeline uses it
automatically.

## Interaction with MIP rules

Once `AstrocyteContext.actor` is populated, MIP rules can match on
identity fields. See
[`examples/mip-enterprise-identity.yaml`](../../astrocyte-py/examples/mip-enterprise-identity.yaml)
for the reference configuration. Matchable fields:

| Field | Example | Notes |
|---|---|---|
| `principal_type` | `user`, `service` | Top-level discriminator. |
| `principal_id` | `admin-oid-9e3c` | Stable identifier. Use sparingly (pin-a-specific-user). |
| `principal_upn` | `alice@corp` | Display identifier. Audit only — do not use as bank key. |
| `principal_app_id` | `hr-bot-client-id` | Service account only. |

Action template variables (for `bank:` / `tags:` interpolation):

| Variable | Resolves to |
|---|---|
| `{principal.type}` | `user` / `service` |
| `{principal.id}` | Stable identifier |
| `{principal.oid}` | Alias for `{principal.id}` (reads well for user-only rules) |
| `{principal.oid_or_app_id}` | Alias for `{principal.id}` regardless of type — use when one rule serves both |
| `{principal.upn}` | Display identifier (user tokens only) |
| `{principal.app_id}` | From `identity.claims["app_id"]` (service only) |

## Bank-prefix convention

The JWT classifier emits `identity.type ∈ {"user", "service"}`. Bank
naming is a **separate** deployment choice — the spec recommends
`user-<oid>` and `svc-<app_id>`, but the existing `BankResolver` uses
`service-<id>`. Pass `service_bank_prefix="svc-"` to `derive_bank_id`
if you prefer the spec's convention.

## Testing

Unit-test the classification rules offline by feeding `classify_jwt_claims`
synthetic claim dicts — no JWT library needed. See
`tests/test_identity_jwt_classifier.py` for the canonical test table
(mirrors the spec §3 Gap 1 test table one-for-one).

For integration tests that exercise the MCP transport, use
`fastmcp.testing` or equivalent to inject a fake `Authorization` header
and assert the resulting `AstrocyteContext.actor` is correct.

## See also

- [Astrocyte Identity Spec](../_design/astrocyte_identity_spec.md) —
  architectural rationale and full gap analysis
- [Proxied User Identity](proxied-user-identity.md) — the
  `beneficiary_user_id` convention for m2m-proxied flows
- [`examples/mip-enterprise-identity.yaml`](../../astrocyte-py/examples/mip-enterprise-identity.yaml)
  — reference routing config for the three caller patterns
