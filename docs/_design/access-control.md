# Access control

Astrocyte enforces who can read, write, reflect, and administer each memory bank. The framework is the natural enforcement point - it sits between every caller and every provider.

Structured identity (**`ActorIdentity`**, **on-behalf-of**, **tenant** fields on `AstrocyteContext`) and **on-behalf-of permission intersection** are specified in [ADR-002](./adr/adr-002-identity-model.md). **OBO** (*on-behalf-of / delegated access*) is **standard terminology in OAuth and IAM**; Astrocyte uses it for authorization semantics, not for a particular OAuth API. This document focuses on principals, grants, and config.

This maps to **Principle 6 (Barrier maintenance)** - controlling what crosses boundaries, applied to identity and authorization, not just content validation.

**External IdPs and policy engines:** To plug in OIDC/SAML/API-key flows and optional enterprise PDPs (OPA, Cerbos, etc.) without coupling the core to vendors, see **`identity-and-external-policy.md`**. For **which packages** implement grants vs PDP adapters vs the reference REST wiring in this repository, see **`identity-and-external-policy.md` §8**.

---

## 1. Access model

### 1.1 Principals

A principal is an identity that accesses memory. Callers usually pass an **opaque string** on `AstrocyteContext.principal` — Astrocyte does not manage user databases or authentication. The framework may also accept a structured **`actor`** (and optional **`on_behalf_of`**) for delegation; see [ADR-002](./adr/adr-002-identity-model.md). Grant rows still match **`type:id`** strings (e.g. `user:calvin`, `agent:support-bot-1`).

```python
# Principal comes from caller context
brain = Astrocyte.from_config("astrocyte.yaml")
ctx = AstrocyteContext(principal="agent:support-bot-1")

hits = await brain.recall("user preferences", bank_id="user-123", context=ctx)
```

Principal format is convention-based:

| Pattern | Example | Use case |
|---|---|---|
| `agent:{id}` | `agent:support-bot-1` | AI agent identity |
| `user:{id}` | `user:calvin` | Human user |
| `service:{id}` | `service:analytics` | Backend service |
| `team:{id}` | `team:support` | Team-level identity |
| `*` | `*` | Any principal (public access) |

### 1.2 Permissions

| Permission | Grants |
|---|---|
| `read` | `recall()` and `reflect()` against the bank |
| `write` | `retain()` into the bank |
| `forget` | `forget()` memories from the bank (selective deletion) |
| `admin` | Bank configuration, export, import, legal hold, delete bank, `forget(scope="all")` / `clear_bank()` |

For a **single** identity (no on-behalf-of), effective permissions for a bank are the **union** of all matching grant rows (same as before).

When **`on_behalf_of`** is set (agent acting for a user), effective permissions for a bank are the **intersection** of: (1) the union of grants matching the **actor**, and (2) the union of grants matching the **on-behalf-of** identity — see [ADR-002](./adr/adr-002-identity-model.md). This prevents privilege escalation via delegation.

### 1.3 Grant structure

```python
@dataclass
class AccessGrant:
    bank_id: str                         # Bank this grant applies to: exact, "*", or a glob ("project:*")
    principal: str                       # Who gets access: exact, "*", a glob ("user:*"), or a group ("team:api")
    permissions: list[str]               # ["read", "write", "forget", "admin"]
```

**Matching.** A `bank_id` or `principal` without `*`, `?` or `[` matches exactly; a lone `*` matches everything. Anything else is an fnmatch pattern, matched case-sensitively on every platform (`fnmatchcase`): `project:*` matches `project:api-1a2b3c` but not `projectx`. Bank ids are restricted to `[a-zA-Z0-9._:@-]`, so a pattern can never be mistaken for a real id. Patterns are never offered as candidates by identity-driven bank resolution; only concrete ids are.

### 1.5 Groups and caller-bound grants

`AstrocyteContext` carries two optional fields set by whatever authenticated the caller, never by the caller itself:

- **`groups`**: group principals the actor belongs to (`team:api`). A grant whose `principal` matches a group applies to its members. `team:x` parses as `ActorIdentity(type="team", id="x")`.
- **`grants`**: extra grants bound to this caller, e.g. by a per-user gateway token (`ASTROCYTE_AUTH_MODE=token`, see the gateway's authentication guide).

**Combination rule.** Caller-bound grants are **added** to the configured grants, then matched exactly like them (bank and principal). Effective permissions for a single identity are therefore the union of: configured grants for the principal, configured grants for its groups, and its caller-bound grants. A token can widen what its principal may do but never narrow the config; to issue a restricted token, keep the principal's config grants narrow. Under on-behalf-of, `groups` extend the actor's side only, and the intersection with the delegating identity still applies (§1.2).

This is the auth model team memory relies on (`team-memory.md` §6): one token per person, bound to a principal, its `team:` groups and `project:*`-style grants.

### 1.4 Sharing memory between users and agents

Many products need **humans and agents to see the same bank**—not a bypass of isolation, but **explicit** overlap in who holds **which permissions** on **`bank_id`**.

- **One bank, multiple principals:** Grant both `user:{id}` and `agent:{id}` **read** and/or **write** on the same bank (with asymmetric permissions if you want—for example user gets **admin**, agent only **read** and **write**, no **forget**).
- **Identity per request:** Each call uses one `AstrocyteContext` — typically one **principal** string, optionally **`actor`** + **`on_behalf_of`** for OBO. Sharing across humans/agents is expressed in **config grants** (or PDP rules).
- **Revocation:** When an agent must lose access, **revoke** or narrow grants (§5); do not rely on the model to “forget” the API.
- **Contrast with accidental access:** If the **wrong** principal or **wrong** bank is chosen—especially from an **untrusted** execution environment—recall becomes an **exfiltration** channel. Bind **identity at the BFF** and align **agent card → principal + bank** mapping with environment/sandbox context; see `sandbox-awareness-and-exfiltration.md`.

Multi-bank orchestration (cascade, parallel) still requires **read** on **every** bank in the query (§3.2); shared memory is “the same bank appears in two principals’ worlds because both are authorized,” not a special API mode—see `multi-bank-orchestration.md` §2.4.

---

## 2. Configuration

### 2.1 Per-bank access in config

```yaml
banks:
  user-123:
    access:
      - principal: "agent:support-bot-1"
        permissions: [read, write]
      - principal: "agent:analytics"
        permissions: [read]
      - principal: "user:calvin"
        permissions: [read, write, forget, admin]

  team-support:
    access:
      - principal: "team:support"
        permissions: [read, write]
      - principal: "agent:*"             # All agents
        permissions: [read]
      - principal: "user:ops-admin"
        permissions: [admin]

  org-policies:
    access:
      - principal: "*"                   # Everyone
        permissions: [read]
      - principal: "user:policy-admin"
        permissions: [read, write, admin]
```

### 2.2 Default access policy

When a bank has no explicit access configuration:

```yaml
access_control:
  enabled: true
  default_policy: owner_only             # "owner_only" | "open" | "deny"
```

| Policy | Behavior |
|---|---|
| `owner_only` | The principal that created the bank gets full access. No one else. |
| `open` | All principals get read/write. Admin requires explicit grant. |
| `deny` | No access until explicitly granted. Fail-safe default. |

### 2.3 Bank templates with access

Bank templates (see `multi-bank-orchestration.md`) can define access patterns:

```yaml
bank_templates:
  user:
    name_pattern: "user-{user_id}"
    access:
      - principal: "agent:{agent_id}"
        permissions: [read, write]
      - principal: "user:{user_id}"
        permissions: [read, write, forget, admin]
      - principal: "service:analytics"
        permissions: [read]
```

Variables (`{user_id}`, `{agent_id}`) are resolved when the bank is created.

---

## 3. Enforcement

### 3.1 Where enforcement happens

Access control is enforced in the **Astrocyte core**, after policy checks but before any provider or pipeline call:

```
caller → brain.recall(query, bank_id, context)
  → Policy: rate limit check
  → Policy: query sanitization
  → ACCESS CONTROL: does ctx.principal have "read" on bank_id?
    → YES: proceed
    → NO: raise AccessDenied(principal, bank_id, "read")
  → Pipeline or Engine: recall
  → Policy: token budget
  → return results
```

### 3.2 Multi-bank access

For multi-bank operations (see `multi-bank-orchestration.md`), the caller must have appropriate permissions on **every** bank in the query:

```python
# This checks "read" on all three banks
hits = await brain.recall(
    query,
    banks=["user-123", "team-support", "org-policies"],
    context=ctx,
)
# Raises AccessDenied if ctx.principal lacks "read" on any bank
```

### 3.3 MCP server context

The MCP server (see `mcp-server.md`) derives the principal from its configuration:

```yaml
mcp:
  principal: "agent:claude-code"         # All MCP tool calls use this identity
```

Or from the MCP client's metadata if available.

---

## 4. Access control and providers

Access control is **framework-level only**. It is not delegated to providers.

- **Tier 1 (Storage)**: The pipeline calls retrieval SPIs with bank_id. The storage layer does not check permissions - Astrocyte already did.
- **Tier 2 (Memory Engine)**: The memory engine receives requests from Astrocyte. It may have its own access control (e.g., Mystique tenant isolation), but Astrocyte' access control is the outer layer.

This means switching providers does not change access policies. Policies are defined in Astrocyte config, not in provider config.

---

## 5. Programmatic access management

> **Status:** The public methods `grant_access()`, `revoke_access()`, `list_access()`,
> and `check_access()` are design targets — not yet implemented. Currently, access
> grants are managed via YAML configuration (§2) and checked internally by the
> framework at each operation boundary via `_check_access()`.

Planned API (not yet available):

```python
# Grant access
await brain.grant_access(
    bank_id="user-123",
    principal="agent:new-bot",
    permissions=["read"],
)

# Revoke access
await brain.revoke_access(
    bank_id="user-123",
    principal="agent:old-bot",
)

# List grants for a bank
grants = await brain.list_access("user-123")

# Check access (without making a memory call)
allowed = await brain.check_access(
    bank_id="user-123",
    principal="agent:support-bot-1",
    permission="write",
)
```

---

## 6. Audit integration

All access control decisions are logged:

- **Granted access**: logged as `access.granted` audit event
- **Denied access**: logged as `access.denied` audit event with principal, bank, and permission
- **Grant/revoke changes**: logged as `access.grant_changed` audit event

These events flow through the standard audit trail (see `memory-lifecycle.md`) and event hooks (see `event-hooks.md`).

---

## 7. Relationship to authentication

Astrocyte does **not** authenticate callers. It receives a principal identifier and trusts it. Authentication is the caller's responsibility:

- **Library usage**: the calling application asserts the principal
- **MCP server**: the MCP config asserts the principal
- **HTTP service** (if wrapped in a web framework): the auth middleware asserts the principal. The reference gateway's `token` mode resolves a per-user token to its principal, groups and grants, and ignores any client-supplied principal header.

**Provenance.** `retain()` stamps `metadata["_actor"]` with the resolved actor whenever a context is present, overwriting any `_actor` the caller put in `metadata`, so a client can't attribute a memory to someone else. Without a context (library imports), a caller-supplied `_actor` is kept; the gateway drops it on anonymous HTTP requests.

This separation follows the same pattern as Kubernetes RBAC (auth middleware → identity → RBAC enforcement) and avoids coupling Astrocyte to any specific auth system.

**Integrating OIDC, SAML, API keys, or commercial IdPs:** map verified credentials to a stable **principal string** (see §1.1) before constructing `AstrocyteContext`. Optional community packages may help map JWT claims or framework-specific request objects to principals; see `identity-and-external-policy.md` §3.

---

## 8. Optional external authorization (PDP)

The default model in this document is **declarative grants** stored in Astrocyte (config or `grant_access`). For enterprises that centralize authorization in a **Policy Decision Point** (OPA, Cerbos, SpiceDB-backed services, Permit.io, etc.), Astrocyte supports an optional **`AccessPolicyProvider`** SPI:

- Invoked at the **same enforcement point** as §3.1 (before pipeline or engine).
- Can **replace** or **chain with** config-based grants (product-defined modes - see `identity-and-external-policy.md` §4.2).
- Implemented in optional packages such as **`astrocyte-access-policy-opa`**, **`astrocyte-access-policy-casbin`** (in-process [Casbin](https://casbin.org/)), or similar, not in the core library.

**Authentication (IdP) examples:** [Casdoor](https://casdoor.org/), Keycloak, Auth0 - you validate credentials **outside** Astrocyte and pass a **principal**; see `identity-and-external-policy.md` §3.

Astrocyte remains responsible for **audit events** (§6) and for **memory-specific** permissions (`read`, `write`, `forget`, `admin`); the external system supplies **allow/deny** (and optional reasons) for those checks.

---

## 9. Summary: adoption-friendly boundaries

| Concern | Where it lives |
|---|---|
| Prove identity (login, token validation) | **Your** middleware, API gateway, or agent host |
| Stable **principal** for memory | String in `AstrocyteContext` |
| Bank-level **permissions** | Default: §2–3; optional: external PDP via §8 |
| Intentional **user + agent** sharing | Same bank, multiple grants (§1.4); templates in §2.3 and `multi-bank-orchestration.md` §2.4, §6 |
| Sandbox / exfiltration vs policy | `sandbox-awareness-and-exfiltration.md` |
| Audit of access decisions | Astrocyte (§6), enriched with PDP metadata when used |

For full integration patterns, package naming, and entry points, see **`identity-and-external-policy.md`**.
