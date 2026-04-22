# Authentication setup

Step-by-step guide for configuring authentication on the Astrocyte standalone gateway (`astrocyte-gateway-py`). Covers all four auth modes, access grants, and integration with OIDC providers.

---

## Choose an auth mode

Set `ASTROCYTE_AUTH_MODE` to select how the gateway authenticates incoming requests:

| Mode | Env var value | Client sends | Maps to | Production? |
|------|--------------|--------------|---------|-------------|
| **Dev** | `dev` (default) | `X-Astrocyte-Principal` header | `principal` | No |
| **API key** | `api_key` | `X-Api-Key` + `X-Astrocyte-Principal` | `principal` | Internal only |
| **JWT HS256** | `jwt_hs256` or `jwt` | `Authorization: Bearer <token>` | `principal` from `sub` claim | Yes (symmetric) |
| **JWT OIDC** | `jwt_oidc` | `Authorization: Bearer <token>` | `principal`, `actor`, `tenant_id` from claims | Yes (enterprise SSO) |

Every mode produces an `AstrocyteContext` that flows to all operations — retain, recall, reflect, forget — and drives access control when enabled.

---

## Mode 1: Dev (default)

Trusts the client-supplied principal header with no verification. Use only for local development and testing.

```bash
# .env
ASTROCYTE_AUTH_MODE=dev
```

Client request:

```bash
curl -X POST http://localhost:8080/v1/retain \
  -H "Content-Type: application/json" \
  -H "X-Astrocyte-Principal: user:alice" \
  -d '{"content": "Alice likes dark mode", "bank_id": "user-alice"}'
```

If the header is omitted, the request proceeds as anonymous (no principal, no access control).

---

## Mode 2: API key

Shared secret verified with constant-time HMAC comparison. The client sends both the key and a principal header.

```bash
# .env
ASTROCYTE_AUTH_MODE=api_key
ASTROCYTE_API_KEY=sk-your-secret-key-here
```

Client request:

```bash
curl -X POST http://localhost:8080/v1/recall \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: sk-your-secret-key-here" \
  -H "X-Astrocyte-Principal: agent:support-bot" \
  -d '{"query": "user preferences", "bank_id": "shared"}'
```

**Errors:**

| Status | Detail | Cause |
|--------|--------|-------|
| 401 | Invalid or missing API key | `X-Api-Key` does not match `ASTROCYTE_API_KEY` |
| 401 | X-Astrocyte-Principal required | `X-Astrocyte-Principal` header missing or empty |
| 500 | ASTROCYTE_API_KEY is not set | Env var not configured |

**When to use:** Service-to-service calls where both sides share a secret. No key rotation mechanism — rotate by redeploying with a new value.

---

## Mode 3: JWT HS256

Symmetric-key JWT verification. The issuer and gateway share a secret. The `sub` claim becomes the principal.

```bash
# .env
ASTROCYTE_AUTH_MODE=jwt_hs256
ASTROCYTE_JWT_SECRET=your-256-bit-secret
ASTROCYTE_JWT_AUDIENCE=astrocyte          # optional but recommended
```

### Generate a test token

```python
import jwt, time

token = jwt.encode(
    {
        "sub": "user:alice",
        "aud": "astrocyte",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    },
    "your-256-bit-secret",
    algorithm="HS256",
)
print(token)
```

### Client request

```bash
curl -X POST http://localhost:8080/v1/recall \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer eyJhbGci..." \
  -d '{"query": "user preferences", "bank_id": "user-alice"}'
```

### Required JWT claims

| Claim | Required | Description |
|-------|----------|-------------|
| `sub` | Yes | Principal string (e.g. `user:alice`, `agent:bot-1`) |
| `aud` | If `ASTROCYTE_JWT_AUDIENCE` is set | Must match exactly |
| `exp` | Recommended | Expiry timestamp |
| `iat` | Recommended | Issued-at timestamp |

If `ASTROCYTE_JWT_AUDIENCE` is not set, the gateway logs a warning and skips audience validation.

**Errors:**

| Status | Detail | Cause |
|--------|--------|-------|
| 401 | Bearer token required | `Authorization` header missing or not `Bearer ...` |
| 401 | Invalid token | Signature invalid, expired, or audience mismatch |
| 401 | Token missing sub | `sub` claim absent or not a string |
| 500 | ASTROCYTE_JWT_SECRET is not set | Env var not configured |

---

## Mode 4: JWT OIDC (RS256 + JWKS)

Asymmetric JWT verification using your IdP's JWKS endpoint. The gateway fetches public keys from the IdP and verifies RS256 signatures — no shared secret needed.

```bash
# .env
ASTROCYTE_AUTH_MODE=jwt_oidc
ASTROCYTE_OIDC_JWKS_URL=https://auth.example.com/.well-known/jwks.json
ASTROCYTE_OIDC_ISSUER=https://auth.example.com
ASTROCYTE_OIDC_AUDIENCE=astrocyte
ASTROCYTE_OIDC_ACTOR_TYPE=user            # optional, default "user"
```

### How it works

1. Client sends `Authorization: Bearer <token>` (RS256-signed by your IdP)
2. Gateway extracts `kid` (key ID) from the JWT header
3. Fetches the matching public key from `ASTROCYTE_OIDC_JWKS_URL` (cached ~10 min)
4. Verifies RS256 signature, `iss`, and `aud` claims
5. Maps claims to a full `AstrocyteContext` with structured identity

### Claim mapping

| JWT claim | Maps to | Default |
|-----------|---------|---------|
| `sub` | `actor.id` and part of `principal` | Required |
| `astrocyte_actor_type` | `actor.type` | `ASTROCYTE_OIDC_ACTOR_TYPE` env var, or `"user"` |
| `astrocyte_principal` | `principal` (explicit override) | Computed as `{actor_type}:{sub}` |
| `tid` or `tenant_id` | `tenant_id` | `null` |
| All other claims | `actor.claims` dict | Stringified; complex types JSON-serialized |

**Example:** A token with `{"sub": "abc-123", "astrocyte_actor_type": "agent", "tid": "tenant-1", "email": "bot@example.com"}` produces:

```python
AstrocyteContext(
    principal="agent:abc-123",
    actor=ActorIdentity(
        type="agent",
        id="abc-123",
        claims={"email": "bot@example.com"},
    ),
    tenant_id="tenant-1",
)
```

### Provider-specific setup

#### Auth0

```bash
ASTROCYTE_OIDC_JWKS_URL=https://YOUR_DOMAIN.auth0.com/.well-known/jwks.json
ASTROCYTE_OIDC_ISSUER=https://YOUR_DOMAIN.auth0.com/
ASTROCYTE_OIDC_AUDIENCE=astrocyte
```

In Auth0 dashboard: create an API with identifier `astrocyte`. Configure your application to request this audience in the token.

#### Keycloak

```bash
ASTROCYTE_OIDC_JWKS_URL=https://keycloak.example.com/realms/YOUR_REALM/protocol/openid-connect/certs
ASTROCYTE_OIDC_ISSUER=https://keycloak.example.com/realms/YOUR_REALM
ASTROCYTE_OIDC_AUDIENCE=astrocyte
```

In Keycloak admin: create a client with `astrocyte` as the audience. Enable "Service Accounts" for machine-to-machine flows.

#### Azure AD (Entra ID)

```bash
ASTROCYTE_OIDC_JWKS_URL=https://login.microsoftonline.com/YOUR_TENANT_ID/discovery/v2.0/keys
ASTROCYTE_OIDC_ISSUER=https://login.microsoftonline.com/YOUR_TENANT_ID/v2.0
ASTROCYTE_OIDC_AUDIENCE=api://astrocyte
```

Register an app in Azure AD, expose an API with scope, and configure `tid` claim mapping for multi-tenant scenarios.

#### Okta

```bash
ASTROCYTE_OIDC_JWKS_URL=https://YOUR_DOMAIN.okta.com/oauth2/default/v1/keys
ASTROCYTE_OIDC_ISSUER=https://YOUR_DOMAIN.okta.com/oauth2/default
ASTROCYTE_OIDC_AUDIENCE=astrocyte
```

In Okta admin: create an authorization server or use the default. Add `astrocyte` as an audience.

#### AWS Cognito

```bash
ASTROCYTE_OIDC_JWKS_URL=https://cognito-idp.REGION.amazonaws.com/POOL_ID/.well-known/jwks.json
ASTROCYTE_OIDC_ISSUER=https://cognito-idp.REGION.amazonaws.com/POOL_ID
ASTROCYTE_OIDC_AUDIENCE=YOUR_APP_CLIENT_ID
```

In Cognito: create a user pool, add an app client, and use the app client ID as the audience.

### Errors

| Status | Detail | Cause |
|--------|--------|-------|
| 401 | Bearer token required | `Authorization` header missing |
| 401 | Invalid OIDC token: ... | Signature, issuer, audience, or expiry validation failed |
| 401 | Token missing sub | `sub` claim absent |
| 500 | ASTROCYTE_OIDC_JWKS_URL is not set | Env var not configured |
| 500 | ASTROCYTE_OIDC_ISSUER and ASTROCYTE_OIDC_AUDIENCE are required | Env vars not configured |

---

## Access grants

Authentication identifies *who* is calling. Access grants control *what* they can do. Enable access control in `astrocyte.yaml`:

```yaml
access_control:
  enabled: true
  default_policy: owner_only     # "owner_only" | "open" | "deny"
```

### Define grants

```yaml
access_grants:
  # Support bot can read and write to any shared bank
  - bank_id: "shared-*"
    principal: "agent:support-bot"
    permissions: [read, write]

  # Alice has full control of her bank
  - bank_id: "user-alice"
    principal: "user:alice"
    permissions: [read, write, forget, admin]

  # All users can read the public bank
  - bank_id: "public"
    principal: "*"
    permissions: [read]
```

### Per-bank grants

```yaml
banks:
  team-engineering:
    access:
      - principal: "agent:code-reviewer"
        permissions: [read]
      - principal: "user:*"
        permissions: [read, write]
```

Top-level `access_grants` and per-bank `banks.*.access` are merged.

### Permissions

| Permission | Operations |
|------------|-----------|
| `read` | `recall()`, `reflect()` |
| `write` | `retain()` |
| `forget` | `forget()` with specific `memory_ids` or `tags` |
| `admin` | Bank config, `forget(scope="all")`, export/import |
| `*` | All of the above |

### Default policies

| Policy | Behavior when no grants match |
|--------|------------------------------|
| `owner_only` | Only the principal whose ID matches the bank pattern can access |
| `open` | Allow all operations (no enforcement) |
| `deny` | Deny all operations |

### On-behalf-of (OBO)

When `identity.obo_enabled: true` and a context has both `actor` and `on_behalf_of`, effective permissions are the **intersection** of grants for both principals. This prevents privilege escalation when an agent acts on behalf of a user.

---

## Admin token

Admin routes (`/v1/admin/*`) use a separate token, independent of the auth mode:

```bash
# .env
ASTROCYTE_ADMIN_TOKEN=admin-secret-here
```

```bash
curl http://localhost:8080/v1/admin/sources \
  -H "X-Admin-Token: admin-secret-here"
```

If `ASTROCYTE_ADMIN_TOKEN` is not set, admin routes are unprotected.

---

## Environment variable summary

| Variable | Required by | Description |
|----------|------------|-------------|
| `ASTROCYTE_AUTH_MODE` | All | Auth mode: `dev`, `api_key`, `jwt_hs256`/`jwt`, `jwt_oidc` |
| `ASTROCYTE_API_KEY` | `api_key` | Shared secret for API key auth |
| `ASTROCYTE_JWT_SECRET` | `jwt_hs256` | Symmetric key for HS256 JWT verification |
| `ASTROCYTE_JWT_AUDIENCE` | `jwt_hs256` (optional) | Expected `aud` claim |
| `ASTROCYTE_OIDC_JWKS_URL` | `jwt_oidc` | JWKS endpoint URL |
| `ASTROCYTE_OIDC_ISSUER` | `jwt_oidc` | Expected `iss` claim |
| `ASTROCYTE_OIDC_AUDIENCE` | `jwt_oidc` | Expected `aud` claim |
| `ASTROCYTE_OIDC_ACTOR_TYPE` | `jwt_oidc` (optional) | Default actor type (default: `user`) |
| `ASTROCYTE_ADMIN_TOKEN` | Admin routes (optional) | Token for `/v1/admin/*` routes |

---

## Further reading

- [Configuration reference](configuration-reference/) — full `astrocyte.yaml` schema including `access_control`, `identity`, and `access_grants`
- [Access control setup](access-control-setup/) — grants, OBO, common patterns
- [Memory API reference](memory-api-reference/) — how `AstrocyteContext` is passed to retain/recall/reflect/forget
- [Production-grade HTTP service](production-grade-http-service/) — TLS, rate limits, CORS, observability checklist
- [ADR-002 Identity model](/design/adr/adr-002-identity-model/) — design rationale for structured identity
- [Access control](/design/access-control/) — design doc for permissions, grants, and policy enforcement
