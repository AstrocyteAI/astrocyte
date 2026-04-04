# Identity integration and external access policy

This document defines how Astrocytes **composes** with authentication (AuthN) and external authorization (AuthZ) systems **without** becoming an identity provider or coupling to any single vendor. For the default in-framework model (opaque principals + YAML grants), see `access-control.md`.

---

## 1. Design goals

| Goal | Approach |
|---|---|
| **Adoption** | Teams using OIDC, SAML, API keys, workload identity, or commercial IdPs should integrate with **patterns and optional thin adapters**, not forks of Astrocytes. |
| **Stability** | Core ships **interfaces + default behavior**; vendor-specific code lives in **`astrocytes-access-policy-*`** or **`astrocytes-identity-*`** optional packages. |
| **Clear boundaries** | Astrocytes **never** replaces your IdP. It **enforces memory permissions** at the framework boundary and emits **audit events** for allow/deny. |
| **Optional central policy** | Enterprises using OPA, **Casbin**, Cedar, SpiceDB, Cerbos, Permit.io, etc. can plug a **policy decision** behind a small SPI while keeping bank semantics and auditing in Astrocytes. |

---

## 2. What Astrocytes does not do

- **Run an OAuth2 / OIDC server**, issue sessions, or store passwords.
- **Validate JWTs or API keys** inside the memory engine by default (that stays in middleware or sidecar).
- **Embed** Keycloak, **Casdoor**, Auth0, Okta, or commercial PDP SDKs in the `astrocytes` core package.

**Casdoor** (open-source IAM / OIDC-SAML-ldap) and similar IdPs are **deployed by you**; Astrocytes only receives the resulting **principal** after your middleware validates tokens or sessions.

Those systems remain **infrastructure**; Astrocytes consumes their **outputs** (identity attributes, allow/deny decisions) through documented extension points.

---

## 3. Authentication: from credential to principal

Astrocytes continues to accept an **opaque `principal` string** (see `access-control.md` §1.1). **Authentication** is the step that runs **before** `AstrocyteContext` is built:

```
HTTP request / MCP session / workload attestation
  → YOUR AuthN layer (middleware, sidecar, agent runtime)
  → verified identity + claims
  → map to principal string (convention: agent:, user:, service:, …)
  → AstrocyteContext(principal=...)
  → brain.retain / recall / …
```

**Integration options (non-exclusive):**

1. **Application code** - After your web framework’s auth dependency runs, set `principal` from `sub`, `email`, or an internal user ID.
2. **Optional helper packages** - e.g. `astrocytes-identity-oidc` (hypothetical) that maps standard JWT claims to a stable principal format; still **your** tokens, **your** issuer validation. If you use **[Casdoor](https://casdoor.org/)** as IdP, the same pattern applies: validate OIDC access tokens from Casdoor (or rely on Casdoor’s application integration), then map `sub` / username / organization claims to `user:{id}` or `agent:{id}` principals.
3. **MCP / agent hosts** - Map the host’s authenticated agent or user to a principal (see `mcp-server.md`); extend with `21` patterns when MCP exposes richer client identity.

The core **trusts** the principal the same way it does today; integration work is **wiring**, not a second memory API.

---

## 4. Authorization: config grants vs. external policy (PDP)

### 4.1 Default: declarative grants in Astrocytes

The **default** authorizer evaluates **per-bank grants** from config or programmatic APIs (`grant_access`, etc.). No external call. This remains the right default for most open-source users.

### 4.2 Optional: `AccessPolicyProvider` SPI

For organizations that **centralize** authorization in a PDP (Policy Decision Point), Astrocytes defines an **optional** interface:

- **Input**: `principal`, `bank_id`, `permission` (`read` | `write` | `forget` | `admin`), and optional **context** (tenant id, request metadata, resource tags - implementation-defined).
- **Output**: allow or deny (and optional reason string for audit).
- **Placement**: Called at the **same enforcement point** as today’s access check - **after** rate limits / sanitization, **before** pipeline or engine (see `access-control.md` §3.1).

**Composition rule:** The SPI can **replace** or **chain with** config grants:

| Mode | Behavior |
|---|---|
| `external_only` | Only the external PDP decides (config grants ignored for enforcement, or used as cache hints - product choice). |
| `config_then_external` | Deny if config denies; if config allows, optionally ask PDP for ABAC attributes. |
| `external_then_config` | PDP for coarse tenant rules; config grants for fine-grained bank ACLs. |

Exact chaining modes are **configuration** on `Astrocyte`; the SPI remains a single `check(...)` surface.

### 4.3 In-process vs remote policy engines

| Style | Examples | `AccessPolicyProvider` adapter |
|---|---|---|
| **Remote / service PDP** | OPA (REST), Cerbos (gRPC/HTTP), Permit API | Network client in **`astrocytes-access-policy-opa`**, etc. |
| **In-process enforcer** | **[Casbin](https://casbin.org/)** (RBAC/ABAC/ReBAC via model + policy files) | Thin wrapper: `check()` calls `enforcer.enforce(subject, object, action)` with Astrocytes’ `principal`, `bank_id`, and `permission` mapped to Casbin tuples - package e.g. **`astrocytes-access-policy-casbin`**. No HTTP hop; policies ship with your app or load from your store. |

Casbin is a common choice when teams want **embedded** authorization with explicit CSV/model files; OPA/Cerbos when policy is **centralized** and shared across services.

### 4.4 Why not put OPA/Cerbos/Casbin inside core?

- Different enterprises standardize on different PDPs or enforcers.
- Network latency, availability, and versioning belong at the **integration** layer (or in your Casbin model lifecycle).
- Keeping adapters in **`astrocytes-access-policy-{name}`** packages preserves a **slim core** and faster security patches per integration.

---

## 5. Packaging and discovery

| Artifact | Role |
|---|---|
| **`astrocytes`** | `AccessPolicyProvider` protocol (optional), default config-based authorizer, audit hooks. |
| **`astrocytes-access-policy-opa`** (example) | Calls OPA REST API with Rego bundles maintained by the user. |
| **`astrocytes-access-policy-cerbos`** (example) | Cerbos gRPC/HTTP SDK adapter. |
| **`astrocytes-access-policy-casbin`** (example) | Wraps a Casbin `Enforcer` for in-process RBAC/ABAC aligned to bank permissions. |
| **`astrocytes-identity-fastapi`** (example) | Optional helpers: FastAPI dependency that validates JWT and sets `AstrocyteContext`. |
| **`astrocytes-identity-casdoor`** (example) | Optional helpers for OIDC flows where Casdoor is the issuer (claim → principal mapping). |

**Entry point group (illustrative):**

```toml
[project.entry-points."astrocytes.access_policies"]
opa = "astrocytes_access_policy_opa:OPAAccessPolicyProvider"
casbin = "astrocytes_access_policy_casbin:CasbinAccessPolicyProvider"
```

**Config sketch (illustrative):**

```yaml
access_control:
  enabled: true
  policy_provider: opa                    # optional; omit for config-only
  policy_provider_config:
    base_url: http://localhost:8181
    policy_package: astrocytes.memory
```

---

## 6. Audit and observability

External PDP decisions must still produce **audit events** compatible with `access-control.md` §6:

- Include `policy_provider` name in structured fields.
- Log **deny** with reason from PDP when available.
- **Never** log secrets, raw tokens, or full claim payloads by default.

---

## 7. Relationship to other docs

| Document | Topic |
|---|---|
| `access-control.md` | Principals, permissions, grants, enforcement order |
| `architecture-framework.md` | Where identity/policy integration sits in the layer model |
| `ecosystem-and-packaging.md` | Entry points and optional package naming |
| `mcp-server.md` | MCP-scoped identity today; extend with §3 above |
| `production-grade-http-service.md` | Reference HTTP service; AuthN/AuthZ checklist |

---

## 8. Where code lives (this monorepo)

This section names **which folders** own **AuthZ wiring** in [`astrocytes-py`](../astrocytes-py/) versus [`astrocytes-services-py`](../astrocytes-services-py/). It is **not** a substitute for the full design in §3–§5 above.

### 8.1 In-framework grants (`AccessGrant` + `set_access_grants`)

| Responsibility | Location |
|----------------|----------|
| **`AccessGrant`**, **`Astrocyte.set_access_grants()`**, **`_check_access()`** | **`astrocytes-py`** — core types and enforcement (`astrocytes/types.py`, `astrocytes/_astrocyte.py`). |
| **Parsing grants from YAML** (e.g. top-level `access_grants`, or consolidating `banks.*.access` from `access-control.md` §2.1 into a flat list) | **`astrocytes-py`** — extend **`AstrocyteConfig`** / **`load_config`** / `_dict_to_config` in `astrocytes/config.py` so config loading produces a **`list[AccessGrant]`** (or a helper that builds it from config). |
| **Calling `set_access_grants()` at process startup** | **`astrocytes-services-py/astrocytes-rest`** — **`astrocytes_rest/brain.py`** (`build_astrocyte()`): after **`Astrocyte(config)`** and **`set_pipeline(...)`**, apply grants from the loaded config. |
| **Trusted `principal` on each HTTP request** (production) | **`astrocytes-rest`** — FastAPI dependencies / middleware: map verified JWT, API key, mTLS identity to **`AstrocyteContext`**, not an unauthenticated client header alone. |

**Not here:** [`astrocytes-pgvector`](../astrocytes-services-py/astrocytes-pgvector/) — it implements **VectorStore** only; it does not participate in access grants.

### 8.2 External PDP (`AccessPolicyProvider`)

| Responsibility | Location |
|----------------|----------|
| **Protocol and enforcement hook** (e.g. call PDP from the same place as grant checks) | **`astrocytes-py`** — define **`AccessPolicyProvider`**, integrate with **`Astrocyte`** (e.g. alongside `_check_access`). *As of this writing the SPI may be documented ahead of full implementation; align code with this doc when landing it.* |
| **Vendor-specific client** (OPA REST, Cerbos SDK, Casbin `Enforcer`, etc.) | **Separate optional packages** — e.g. **`astrocytes-access-policy-opa`**, **`astrocytes-access-policy-cerbos`** (see §5). Keeps **`astrocytes`** free of third-party PDP SDKs. |
| **Instantiate adapter + attach to `Astrocyte`** | **`astrocytes-services-py/astrocytes-rest`** — **`brain.py`** (or equivalent factory): resolve provider via config entry point / env, same pattern as **`vector_store`** in **`astrocytes_rest/wiring.py`**. |

**Not here:** **`astrocytes-pgvector`** — unchanged; storage adapters are not AuthZ layers.

### 8.3 Summary table

| Approach | `astrocytes-py` | `astrocytes-rest` | Optional `astrocytes-access-policy-*` |
|----------|-----------------|-------------------|----------------------------------------|
| **Grants** | Config model + load → `AccessGrant` list; `set_access_grants` API | Call `set_access_grants` at startup; later, AuthN → principal | — |
| **External PDP** | SPI + `Astrocyte` integration | Wire provider at startup | OPA / Cerbos / Casbin / … adapters |

---

## 9. Summary

- **AuthN** → **your** middleware or optional **`astrocytes-identity-*`** helpers → **principal string**.
- **AuthZ** → **default** YAML grants, or **optional** **`AccessPolicyProvider`** + **`astrocytes-access-policy-*`** for OPA, Cerbos, **Casbin**, etc.
- **Astrocytes** stays the **memory barrier** and **audit anchor**; IdPs and PDPs stay **pluggable**.
- **Repository placement** for grants vs PDP vs storage: see **§8** above.
