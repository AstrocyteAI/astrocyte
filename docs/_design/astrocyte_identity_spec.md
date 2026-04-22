# Astrocyte — User Authorized Agents
## Identity Resolution, Per-User Bank Routing & Proxied Identity Patterns

**Document:** Astrocyte Identity Architecture Specification  
**Version:** 0.1 — Draft  
**Status:** For review  
**Date:** April 2026

---

## 1. Context

Enterprise AI agents operate on behalf of human users. When an agent retains or recalls memories, the memory must be attributed to the right identity — the human user the agent is acting for, not the agent itself or the machine credential it used to authenticate.

This document defines Astrocyte's approach to identity resolution for three distinct caller patterns that arise in any enterprise deployment:

**Regulatory context:**

Correct per-user identity resolution is not only an architectural concern — it is a compliance requirement in multiple jurisdictions. The following frameworks impose specific obligations on how AI-retained data is attributed, governed, and subject to individual rights:

| Framework | Jurisdiction | Key obligation for AI memory |
|---|---|---|
| **PDPA** | Singapore | Personal data must be attributed to the individual it concerns. Data subjects have access and erasure rights. |
| **MAS TRM** | Singapore (financial institutions) | Customer interaction records must be maintained with individual attribution for 5–7 years. |
| **MAS Notice 626** | Singapore (banks) | Customer data handling requires audit trails attributable to the specific customer. |
| **IM8** | Singapore (government agencies) | Classified and sensitive data must be attributed to the individual it concerns and handled under defined access controls. |
| **GDPR** | European Union | Data subjects have rights of access, rectification, and erasure. Personal data must be attributable to the specific individual. |
| **HIPAA** | United States (healthcare) | Patient data must be attributed and accessible for individual rights requests. Minimum 6-year retention for covered records. |

Deploying AI agents without correct per-user memory attribution creates direct exposure under all of the above frameworks — particularly in the m2m-proxied scenario where personal data is retained under a machine identity rather than the individual it concerns.

---

**m2u (machine-to-user):** A human user triggers an agent via a frontend application. The agent acts on behalf of that user. User identity flows through the chain via a delegated token (e.g. OAuth 2.0 On-Behalf-Of / OBO). The agent's memory operations must be attributed to the specific human user, isolated from all other users.

**m2m (machine-to-machine):** A background system — a batch job, data pipeline, or integration service — calls an agent using its own service account credentials. No human user identity is in the chain. Memory operations are attributed to the service identity and held separately from all human user banks.

**m2m-proxied:** An agent acts on behalf of a human user at the orchestration layer, but calls a downstream system that does not support delegated user tokens. The agent holds the user's identity (from the OBO token) but cannot pass it downstream. The user identity must be explicitly declared by the calling layer as metadata, not derived from a token.

---

## 2. Gap Mapping Table

The following table maps each identity-related requirement against Astrocyte's current capabilities.

| # | Requirement | Astrocyte today | Status | Gap |
|---|---|---|---|---|
| R1 | Per-user bank isolation — each human user's memories isolated from all others | `bank_id` string key supports any isolation scheme | ✅ Supported | None — mechanism exists |
| R2 | Static `bank_id` from config or caller | `bank_id` accepted in MCP config and per-call metadata | ✅ Supported | None |
| R3 | Dynamic `bank_id` auto-resolved from a delegated user token (e.g. OBO JWT) | No JWT inspection in `astrocyte-mcp`. Caller must pass `bank_id` explicitly | ❌ Not implemented | **JWT Identity Middleware required** |
| R4 | Service account (m2m) bank isolation | `bank_id` can be set to `svc-{id}` manually by caller | ✅ Partial | Manual only — no auto-resolution from token claims |
| R5 | Automatic detection of token type (delegated user vs service account) at MCP layer | No token inspection exists | ❌ Not implemented | Depends on R3 / Gap 1 |
| R6 | MIP routing rules keyed on caller identity type (m2u vs m2m) | MIP rules match on `content_type`, tags, topic classifiers — not on principal type | ❌ Not implemented | Requires R3 + new `PrincipalTypeMatcher` |
| R7 | Different retention policies for m2u vs m2m (lifecycle, PII scan, bank) | MIP supports different actions per rule — but cannot yet match on principal type | ✅ Partial | Blocked by R5 / R6 |
| R8 | User attribution for downstream systems with no delegated token support | No convention for declaring a beneficiary user ID in a proxied m2m call | ❌ Not implemented | **`beneficiary_user_id` metadata convention + MIP rule** |
| R9 | PDPA / GDPR audit trail per user (not per machine) for proxied calls | Audit trail records exist per bank — proxied calls land in machine bank, not user bank | ✅ Partial | Blocked by R8 |
| R10 | DSAR handling for memories retained via proxied systems | DSAR tombstone + PII scrub operates per bank — if data is in machine bank it won't surface on a user DSAR | ✅ Partial | Blocked by R8 |
| R11 | Reference `mip.yaml` for m2u / m2m / m2m-proxied routing patterns | No reference configuration exists | ❌ Not implemented | Documentation + reference config |
| R12 | Custom orchestration API integration pattern (Option B) | Astrocyte accepts any `bank_id` and any metadata — compatible with Option B pattern | ✅ Compatible | Integration pattern documentation needed |

---

## 3. Gap Specifications

### Gap 1 — JWT Identity Middleware
**Closes:** R3, R4, R5

#### Problem

`astrocyte-mcp` accepts MCP requests without inspecting the Authorization header. The `bank_id` used for every memory operation is either the statically configured default or whatever the calling agent explicitly passes. This creates three failure modes:

1. If the agent does not explicitly set `bank_id`, all users' memories merge into the same default bank — a critical data isolation failure.
2. If the agent must set `bank_id` explicitly, it must first resolve the user's identity itself — re-introducing the identity responsibility that the memory layer should own.
3. There is no way for Astrocyte to distinguish a delegated user token from a service account credential. Both arrive as Bearer tokens. Without inspection, they are indistinguishable.

#### Solution

Add a JWT identity middleware to the `astrocyte-mcp` request pipeline. On every incoming MCP request, before any memory operation executes:

1. Inspect the `Authorization: Bearer <token>` header
2. Decode and validate the JWT against the IdP's public keys (JWKS endpoint)
3. Classify the token type from standard claims
4. Derive `bank_id` and a typed `Identity` object
5. Stamp the resolved identity onto the request context
6. Pass to the memory pipeline — which uses the resolved identity if no explicit `bank_id` was provided by the caller

Explicit `bank_id` from the caller takes precedence. This preserves full backward compatibility — deployments that do not enable the middleware continue to work unchanged.

#### Implementation

```python
from dataclasses import dataclass, field
from typing import Literal
import jwt
import httpx

IdentityType = Literal["user", "service_principal", "anonymous"]

@dataclass
class Identity:
    type: IdentityType
    bank_id: str
    # User (delegated token) fields
    oid: str | None = None          # Object ID — immutable, use as primary key
    upn: str | None = None          # User Principal Name — for display and audit
    # Service account fields
    app_id: str | None = None       # Application / client ID
    # Common
    tenant_id: str | None = None
    raw_claims: dict = field(default_factory=dict, repr=False)  # audit only


class JWTIdentityMiddleware:
    """
    Resolves caller identity from a Bearer token on every MCP request.

    Supports:
    - Delegated user tokens (OBO / authorization code flow)
    - Service account tokens (client credentials flow)
    - Anonymous callers (no Authorization header, if permitted by config)

    Always fails CLOSED on invalid tokens. A malformed token is never
    silently treated as anonymous — that would risk cross-user data leakage.
    """

    def __init__(self, config: AstrocyteConfig):
        self.config = config
        self._jwks_client = jwt.PyJWKClient(config.identity.jwks_uri)

    def resolve(self, request: MCPRequest) -> Identity:
        auth_header = request.headers.get("Authorization", "")

        if not auth_header.startswith("Bearer "):
            if not self.config.identity.allow_anonymous:
                raise AuthorizationError(
                    "No Bearer token presented and anonymous access is disabled."
                )
            return Identity(
                type="anonymous",
                bank_id=self.config.identity.default_bank_id,
            )

        token = auth_header.removeprefix("Bearer ")

        try:
            claims = self._decode_and_validate(token)
        except jwt.InvalidTokenError as exc:
            # Token presented but invalid — FAIL CLOSED.
            # Never silently fall through to a default bank.
            raise AuthorizationError(
                f"Bearer token is invalid: {exc}. "
                "Memory operation rejected to prevent cross-user data leakage."
            )

        return self._classify(claims)

    def _classify(self, claims: dict) -> Identity:
        """
        Classify token type from standard JWT claims.

        Delegated user token signals (OAuth 2.0 authorization code / OBO):
          - 'upn' claim present (Entra ID / AAD)
          - 'preferred_username' claim present (OIDC standard)
          - 'email' claim present (some IdPs)
          - 'sub' present without 'appid' (general OIDC user token)

        Service account token signals:
          - 'idtyp' == 'app' (Entra ID v2 explicit signal)
          - 'appid' or 'azp' present without user claims
          - 'client_id' present (some IdPs)
        """

        # Delegated user token
        user_identifier = (
            claims.get("upn")
            or claims.get("preferred_username")
            or claims.get("email")
        )
        if user_identifier and claims.get("idtyp") != "app":
            oid = claims.get("oid") or claims.get("sub")
            return Identity(
                type="user",
                bank_id=f"user-{oid}",   # use OID / sub — stable, never use email/UPN
                oid=oid,
                upn=user_identifier,
                tenant_id=claims.get("tid"),
                raw_claims=claims,
            )

        # Service account token
        if (
            claims.get("idtyp") == "app"
            or "appid" in claims
            or "client_id" in claims
        ):
            app_id = (
                claims.get("appid")
                or claims.get("azp")
                or claims.get("client_id")
            )
            return Identity(
                type="service_principal",
                bank_id=f"svc-{app_id}",
                app_id=app_id,
                tenant_id=claims.get("tid"),
                raw_claims=claims,
            )

        # Token decoded but type unrecognised — fail closed
        raise AuthorizationError(
            "Token decoded but identity type could not be determined. "
            "Expected delegated user token or service account credential."
        )

    def _decode_and_validate(self, token: str) -> dict:
        signing_key = self._jwks_client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience=self.config.identity.token_audience,
            options={"require": ["exp", "iat", "sub"]},
        )
```

#### Why OID / sub — not email or username — as the bank key

The `bank_id` must be stable for the lifetime of a user's identity. Email addresses and usernames change. The subject identifier (`oid` in Entra, `sub` in standard OIDC) is immutable and is the correct key.

| Identifier | Stability | Risk if used as bank key |
|---|---|---|
| Email / UPN | Changes on name change, org restructure | Bank becomes orphaned — user loses access to their memories |
| Username | Changes on rename | Same orphan risk |
| `oid` / `sub` | Immutable for the lifetime of the identity | Safe — stable permanent key |

Record the human-readable identifier (email, UPN) in the audit trail for readability. Use the immutable identifier as the bank key.

#### `astrocyte.yaml` additions

```yaml
identity:
  jwt_middleware:
    enabled: true
    jwks_uri: "https://your-idp.example.com/.well-known/jwks.json"
    token_audience: "api://your-astrocyte-app-id"
    token_issuer: "https://your-idp.example.com/"  # validated if set
    fail_closed: true         # reject requests with invalid tokens (recommended)
    allow_anonymous: false    # permit calls with no Authorization header
    jwks_refresh_interval_hours: 24

  # Fallback when anonymous access is permitted
  default_bank_id: "default"
```

#### Test cases

| Scenario | Expected result |
|---|---|
| Valid delegated user token | `Identity(type="user", bank_id="user-{oid}")` |
| Valid service account token | `Identity(type="service_principal", bank_id="svc-{app_id}")` |
| No Authorization header, `allow_anonymous: true` | `Identity(type="anonymous", bank_id=default_bank_id)` |
| No Authorization header, `allow_anonymous: false` | `AuthorizationError` |
| Malformed token | `AuthorizationError` (fail closed — never silent) |
| Expired token | `AuthorizationError` |
| Wrong audience | `AuthorizationError` |
| Wrong issuer | `AuthorizationError` |
| Token decoded but type unrecognised | `AuthorizationError` |

**Effort:** 2–3 days  
**Dependencies:** `PyJWT[cryptography]`, `httpx`. No changes to retain / recall / reflect pipeline.

---

### Gap 2 — `PrincipalTypeMatcher` in MIP
**Closes:** R5, R6

#### Problem

MIP rule matching currently operates on content fields: `content_type`, `topic`, `tags`, `source`. Rules cannot currently branch on who is calling. Once Gap 1 is implemented and every request carries a typed `Identity`, MIP needs one new matcher so rules can route on caller identity type.

#### Solution

```python
class PrincipalTypeMatcher:
    """
    Matches MIP rules against the resolved identity type.
    Requires JWT Identity Middleware (Gap 1) to be enabled.

    Supported match conditions:
      principal_type: user | service_principal | anonymous
      principal_oid: <specific user OID>        (optional filter)
      principal_app_id: <specific app ID>        (optional filter)
    """

    SUPPORTED_FIELDS = frozenset({
        "principal_type",
        "principal_oid",
        "principal_app_id",
    })

    def matches(self, rule: MIPRule, request: MCPRequest) -> bool:
        conditions = rule.match
        identity = getattr(request.context, "identity", None)

        # Rule has no principal constraints — matches all callers
        if not any(f in conditions for f in self.SUPPORTED_FIELDS):
            return True

        # Identity not resolved (middleware not enabled) — skip rule
        if identity is None:
            return False

        if "principal_type" in conditions:
            if conditions["principal_type"] != identity.type:
                return False

        # Optional: restrict rule to a specific user
        if "principal_oid" in conditions:
            if identity.oid != conditions["principal_oid"]:
                return False

        # Optional: restrict rule to a specific service account
        if "principal_app_id" in conditions:
            if identity.app_id != conditions["principal_app_id"]:
                return False

        return True
```

#### MIP action template variables added

| Variable | Resolves to | Available for |
|---|---|---|
| `{principal.oid}` | User's immutable subject identifier | `type: user` |
| `{principal.upn}` | User's display identifier (email/UPN) | `type: user` |
| `{principal.app_id}` | Service account application ID | `type: service_principal` |
| `{principal.oid_or_app_id}` | OID for users, app_id for service accounts | All |
| `{principal.type}` | `user`, `service_principal`, or `anonymous` | All |

**Effort:** 0.5 day (after Gap 1 is merged)

---

### Gap 3 — Reference `mip.yaml` for m2u / m2m / m2m-proxied routing
**Closes:** R6, R7, R11

#### Problem

No reference routing configuration exists that demonstrates how to encode the m2u / m2m / m2m-proxied distinction in MIP rules. This is a documentation gap, not a code gap — but without a concrete reference, integrators will either duplicate routing logic in agent system prompts or route everything to a single bank.

#### Reference configuration

```yaml
# mip.yaml — reference configuration for enterprise deployments
# Covers m2u (delegated user token), m2m (service account),
# and m2m-proxied (user data via service account credential) flows.
# Requires: JWT Identity Middleware enabled, PrincipalTypeMatcher active.

rules:

  # ── COMPLIANCE OVERRIDES ─────────────────────────────────────
  # These fire before any identity-based routing.
  # override: true means the MIP LLM layer cannot change this decision.

  - name: pii-hard-route
    priority: 100
    match:
      pii_detected: true
    action:
      bank: "private-encrypted-{principal.oid_or_app_id}"
      tags: [pii, encrypted]
      pii_scan: true
      encrypt: true
      audit_level: full
      override: true

  - name: sensitive-classification
    priority: 99
    match:
      classification: [restricted, confidential, secret]
    action:
      encrypt: true
      tags: [classified]
      audit_level: full
      override: true

  # ── M2U: Delegated user token ─────────────────────────────────
  # Agent is acting on behalf of a human user.
  # Full PDPA / GDPR lifecycle. PII scan mandatory.
  # Retention guidance:
  #   PDPA (Singapore): retain only as long as necessary for the purpose collected
  #   MAS TRM (financial institutions): interaction records typically 5–7 years
  #   MAS Notice 626 (banks): customer data handling and access audit requirements
  #   HIPAA (US healthcare): 6-year minimum for covered records
  #   IM8 (Singapore government): classified data handling and retention obligations
  # Set retention_days to match the most restrictive applicable obligation.

  - name: user-personal-interaction
    priority: 10
    match:
      principal_type: user
      content_type: [query_response, preference, interaction, conversation]
    action:
      bank: "user-{principal.oid}"
      tags: [user, m2u, personal]
      retention_days: 365
      pii_scan: true
      audit_level: full

  - name: user-document-context
    priority: 10
    match:
      principal_type: user
      content_type: [document_summary, research_note, decision]
    action:
      bank: "user-{principal.oid}"
      tags: [user, document]
      retention_days: 730
      pii_scan: true
      audit_level: full

  # ── M2M: Service account / batch operations ───────────────────
  # No human user in the chain. Shorter retention. No personal attribution.

  - name: machine-operational-events
    priority: 10
    match:
      principal_type: service_principal
      content_type: [pipeline_event, batch_result, system_log, sync_event]
    action:
      bank: "svc-{principal.app_id}"
      tags: [m2m, operational, system]
      retention_days: 90
      pii_scan: false
      audit_level: standard

  - name: machine-integration-outputs
    priority: 10
    match:
      principal_type: service_principal
      content_type: [integration_result, api_response]
    action:
      bank: "svc-{principal.app_id}"
      tags: [m2m, integration]
      retention_days: 30
      pii_scan: false
      audit_level: standard

  # ── M2M-PROXIED: User data via service account credential ─────
  # The downstream system does not support delegated tokens.
  # The calling layer MUST declare beneficiary_user_id explicitly.
  # pii_scan MUST be true — these systems return personal data.
  # Memory is routed to the user's bank, not the machine bank.

  - name: proxied-user-data
    priority: 8
    match:
      principal_type: service_principal
      metadata.beneficiary_user_id: present
    action:
      bank: "user-{metadata.beneficiary_user_id}"
      tags: [proxied, m2m-on-behalf, personal]
      retention_days: 365
      pii_scan: true
      audit_level: full
      audit_note: >
        Memory retained via service account proxy.
        Downstream system does not support delegated user tokens.
        Beneficiary declared explicitly by calling orchestration layer.
        Proxy service account: {principal.app_id}

  # ── CATCH-ALL ─────────────────────────────────────────────────
  # Service account call with no beneficiary declaration.
  # Data lands in machine bank — NOT user bank.
  # This is intentional: if the caller does not declare a beneficiary,
  # Astrocyte will not guess. Data has no personal attribution.

  - name: machine-default
    priority: 1
    match:
      principal_type: service_principal
    action:
      bank: "svc-{principal.app_id}"
      tags: [m2m, unattributed]
      retention_days: 30
      pii_scan: true   # scan even unattributed — unexpected PII should surface
      audit_level: standard
      audit_note: >
        Warning: service account call with no beneficiary_user_id declared.
        Memory routed to machine bank. Personal data will not be user-attributed.
        If this agent acts on behalf of a human, add beneficiary_user_id to metadata.
```

**Effort:** 1 day (documentation + config). No code changes if Gaps 1 and 2 are implemented.

---

### Gap 4 — `beneficiary_user_id` Convention
**Closes:** R8, R9, R10, R12

#### Problem

Some downstream systems authenticate machine-to-machine only — they accept no delegated user token regardless of what the calling agent holds. The data those systems return is often personal (leave balances, HR records, support tickets about a specific person). Without a convention for declaring which user the data belongs to, that memory:

- Lands in the machine bank, not the user's bank
- Does not surface when the user's agent performs recall queries
- Is invisible to DSAR — the user requesting their data will not find it
- Cannot be tombstoned as part of the user's right-to-erasure request
- Has wrong PDPA attribution — personal data retained without user-level governance
- Cannot satisfy MAS TRM audit requirements for financial institution deployments — MAS requires institutions to maintain records of customer data access and retention at the individual customer level
- Cannot satisfy MAS Notice 626 obligations for banks — customer interaction data must be attributable to the specific customer
- Cannot satisfy IM8 requirements for Singapore government deployments — sensitive data must be attributed to the individual it concerns

#### Solution

Formalise `beneficiary_user_id` as a documented first-class metadata field in the Astrocyte SDK. When an agent acts on behalf of a user but cannot obtain a user token from the downstream system, the calling orchestration layer must declare the user's identity explicitly in the `retain()` metadata.

This is a **convention with documented consequences**, not a technical enforcement. Astrocyte cannot infer which human a proxied machine call is for — the calling layer must own that declaration.

#### SDK contract

```python
await astrocyte.retain(
    content="Annual leave balance: 5 days remaining",
    # Do NOT set bank_id explicitly — let MIP route based on beneficiary
    metadata={
        # REQUIRED when acting on behalf of a user via service account credential
        "beneficiary_user_id": "sub-or-oid-of-the-human-user",

        # RECOMMENDED — helps MIP apply source-specific routing rules
        "source_system": "hr-system-name",

        # RECOMMENDED — documents why a user token was not available
        "proxy_reason": "Downstream system does not support delegated tokens",

        # OPTIONAL — links back to the delegated session that triggered this
        "originating_session_id": "session-xyz-abc",
    }
)
```

#### What happens when `beneficiary_user_id` is declared

1. MIP `proxied-user-data` rule matches (`principal_type: service_principal` + `metadata.beneficiary_user_id: present`)
2. Memory is routed to `user-{beneficiary_user_id}` bank — the user's bank
3. Full PDPA lifecycle applied (365 days retention, PII scan, full audit)
4. Audit note records the proxy chain — service account ID, declared beneficiary
5. User's future recall queries surface this memory
6. DSAR for the user includes this memory
7. Right-to-erasure (tombstone + PII scrub) covers this memory

#### What happens when `beneficiary_user_id` is absent

The machine-default catch-all rule applies. Memory lands in the machine bank. Data is NOT user-attributed. A warning is written to the audit log. The data will NOT surface on the user's DSAR.

This is intentional. Astrocyte will not guess which user a proxied call was for. The calling layer is responsible for the declaration.

#### The custom orchestration API pattern (Option B)

The `beneficiary_user_id` convention works correctly when the calling layer has access to both the user's delegated token (at the orchestration layer) and a service account credential (for the downstream system). The calling layer extracts the user's immutable subject identifier from the delegated token and passes it to Astrocyte.

```python
# In the custom orchestration API
# This layer holds BOTH the user's delegated token AND the service account credential

async def get_data_on_behalf_of_user(delegated_token: str) -> Any:
    # 1. Extract the user's immutable subject identifier from the delegated token
    user_claims = decode_jwt(delegated_token)
    user_subject_id = user_claims["sub"]  # or "oid" for Entra ID

    # 2. Call the downstream system with the service account credential
    #    (The downstream system does not accept delegated tokens)
    result = await downstream_system_client.get_data(
        user_reference=resolve_downstream_user_id(user_subject_id)
    )

    # 3. Retain in Astrocyte with explicit beneficiary declaration
    await astrocyte.retain(
        content=format_result(result),
        metadata={
            "beneficiary_user_id": user_subject_id,
            "source_system": "downstream-system-name",
            "proxy_reason": "Downstream system does not support delegated tokens",
        }
    )

    return result
```

**Effort:** 1 day — SDK documentation update, metadata schema addition, `mip.yaml` catch-all rule and proxied rule (above). No new code.

---

## 4. Dependency Graph

```
Gap 1: JWT Identity Middleware
  Implements: Identity dataclass, token parser, JWKS validation
  Adds to:    astrocyte-mcp — JWTIdentityMiddleware
  Config:     astrocyte.yaml identity section
       │
       ├──► Gap 2: PrincipalTypeMatcher
       │      Implements: PrincipalTypeMatcher class
       │      Registers:  matcher in MIP rule engine
       │      Adds:       {principal.*} template variables
       │
       └──► Gap 3: Reference mip.yaml
              Requires:   Gap 1 + Gap 2
              Delivers:   m2u / m2m / m2m-proxied routing reference config
              Documents:  Routing patterns, audit note conventions

Gap 4: beneficiary_user_id convention
  Independent of Gaps 1–3 (works with static bank_id too)
  Delivers:   SDK metadata schema, Option B integration pattern
  Closes:     Data attribution gap for systems with no delegated token support
```

Gaps 1 and 4 are independent of each other and can be developed in parallel. Gap 1 is the prerequisite for Gaps 2 and 3.

---

## 5. Implementation Path

### Phase 0 — Prerequisites (no code)

| Action | Why |
|---|---|
| Register Astrocyte MCP server as an application in the organisation's IdP | JWT middleware needs a registered `token_audience` to validate tokens against |
| Define the delegated token scope (e.g. `api://astrocyte-mcp/Memory.ReadWrite`) | Calling agents must request this scope when obtaining tokens to present to Astrocyte |
| Confirm JWKS URI for the deployment | Required for `astrocyte.yaml` `jwks_uri` configuration |
| Confirm the stable user subject claim name in the IdP's tokens (`oid`, `sub`, etc.) | Determines which claim to use as the bank key in `_classify()` |
| Confirm whether anonymous callers are permitted in this deployment | Determines `allow_anonymous` configuration |

---

### Phase 1 — JWT Identity Middleware

**Files:**
- `astrocyte_mcp/middleware/jwt_identity.py` — `Identity` dataclass, `JWTIdentityMiddleware`
- `astrocyte_mcp/server.py` — wire middleware into request pipeline
- `astrocyte/config/schema.py` — add `identity` section to `AstrocyteConfig`
- `tests/mcp/test_jwt_identity.py` — full test coverage (see test table in Gap 1 spec)

**Acceptance criteria:**
- Delegated user token → `bank_id: user-{sub_or_oid}` without caller setting it
- Service account token → `bank_id: svc-{app_id}` without caller setting it
- Invalid token → `AuthorizationError` raised, request rejected, no memory operation executed
- Anonymous caller with `allow_anonymous: false` → `AuthorizationError`
- Explicit `bank_id` from caller overrides middleware resolution (backward compat)
- Middleware disabled (`enabled: false`) → existing static behaviour unchanged

**Effort:** 2–3 days

---

### Phase 2 — PrincipalTypeMatcher in MIP

**Files:**
- `astrocyte/mip/matchers/principal_type.py` — `PrincipalTypeMatcher`
- `astrocyte/mip/engine.py` — register matcher in chain
- `astrocyte/mip/actions.py` — `{principal.*}` template variable rendering
- `tests/mip/test_principal_type_matcher.py`

**Acceptance criteria:**
- User token request matches `principal_type: user` rule; does not match `principal_type: service_principal` rule
- Service account request matches `principal_type: service_principal` rule; does not match `principal_type: user` rule
- Rule with no `principal_type` constraint matches all callers
- `{principal.oid}` template variable resolves correctly in `bank` action field
- `{principal.app_id}` template variable resolves correctly for service accounts
- MIP rule engine operates identically when JWT middleware is disabled

**Effort:** 0.5 day (after Phase 1 merged)

---

### Phase 3 — Documentation

**Files:**
- `docs/examples/enterprise/mip.yaml` — reference routing config (Gap 3 above)
- `docs/guides/proxied-user-identity.md` — `beneficiary_user_id` convention (Gap 4)
- `docs/guides/jwt-identity-middleware.md` — configuration and IdP setup guide
- `docs/compliance/dsar-and-proxied-memory.md` — DSAR implications for proxied flows

**Effort:** 1 day

---

### Phase 4 — Integration Testing

| Test | What it validates |
|---|---|
| **m2u end-to-end** | User token in Authorization header → memory in `user-{oid}` bank → second user's query returns no results |
| **m2m end-to-end** | Service account token → memory in `svc-{app_id}` bank → isolated from all user banks |
| **m2m-proxied end-to-end** | Service account call + `beneficiary_user_id` → memory in user bank → surfaces on user recall and DSAR |
| **Missing beneficiary** | Service account call, no `beneficiary_user_id` → machine bank, audit warning, absent from user recall |
| **Token fail-closed** | Invalid token presented → `AuthorizationError`, no memory written |
| **Cross-user isolation** | User A's memory not visible to User B's agent |
| **DSAR coverage** | User DSAR surfaces direct m2u memories AND proxied memories where beneficiary was declared |

**Effort:** 1–2 days

---

## 6. Effort Summary

| Phase | What | Effort |
|---|---|---|
| 0 | Prerequisites — IdP registration, config values | 0.5 day |
| 1 | JWT Identity Middleware | 2–3 days |
| 2 | PrincipalTypeMatcher in MIP | 0.5 day |
| 3 | Documentation | 1 day |
| 4 | Integration testing | 1–2 days |
| **Total** | | **~5–7 days** |

---

## 7. Design Decisions Log

| Decision | Rationale |
|---|---|
| Use immutable subject ID (`oid` / `sub`) not email as bank key | Email addresses and usernames change. Immutable subject IDs do not. Bank key must be stable for the lifetime of the user's identity. |
| Fail closed on invalid tokens | A malformed token is never silently treated as anonymous. Silent fallthrough would risk routing authenticated user data to a shared default bank. |
| `beneficiary_user_id` is a convention, not enforcement | Astrocyte cannot infer which human a proxied machine call is for. The calling orchestration layer holds both the user identity and the machine credential — it is the only layer that can make this declaration. |
| Explicit `bank_id` from caller overrides middleware | Preserves full backward compatibility. Deployments not using JWT middleware continue to work. |
| `pii_scan: true` on all proxied rules | Systems that don't support delegated tokens (HR, ITSM, finance) typically return personal data. Scanning is mandatory regardless of whether the caller expected PII. |
| Catch-all machine-default rule writes an audit warning | Silent routing to machine bank without warning would make the absence of `beneficiary_user_id` invisible to operators. The warning makes the gap detectable in audit logs. |
| JWT middleware opt-in via config | Deployments that do not use an IdP should not be broken by new code. The middleware is disabled by default. |
| Tombstone (not cascade delete) as default on right-to-erasure | PDPA right-to-erasure and GDPR Article 17 require erasure, but MAS TRM and MAS Notice 626 require financial institutions to retain interaction records for 5–7 years. Tombstone + PII scrub satisfies both — the personal identifiers are removed (erasure obligation met) while the structural record and audit entry remain (legal hold obligation met). Cascade delete is available as a privileged operator action for cases with no legal hold. |

---

## 8. Open Questions

| # | Question | Impact |
|---|---|---|
| Q1 | What is the stable user subject claim in the target IdP's tokens? (`oid`, `sub`, or other) | Determines claim used as bank key in `_classify()` |
| Q2 | Should the middleware support multiple IdPs / JWKS URIs in a single deployment (multi-tenant scenarios)? | Affects JWKS client architecture — single vs list of trusted issuers |
| Q3 | What is the token audience convention for the organisation's Astrocyte deployment? | Required for `token_audience` config and validation |
| Q4 | For deployments where the calling agent is a managed service (not a custom API), how is `beneficiary_user_id` propagated? | May require agent framework-level support for metadata passthrough |
| Q5 | Should `beneficiary_user_id` accept both OID/sub and display identifiers, with Astrocyte normalising internally? | Would simplify calling code but adds lookup complexity in Astrocyte |
| Q6 | Does the MIP catch-all machine-default rule's audit warning need to be surfaced in the Cerebro dashboard as a compliance alert? | Determines whether silent unattributed proxy calls are visible to compliance teams in real time |

---

*Astrocyte Identity Architecture Specification. For integration with specific organisational IdP configurations, a separate deployment guide should be maintained referencing this spec.*
