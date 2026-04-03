# Access control

Astrocytes enforces who can read, write, reflect, and administer each memory bank. The framework is the natural enforcement point - it sits between every caller and every provider.

This maps to **Principle 6 (Barrier maintenance)** - controlling what crosses boundaries, applied to identity and authorization, not just content validation.

**External IdPs and policy engines:** To plug in OIDC/SAML/API-key flows and optional enterprise PDPs (OPA, Cerbos, etc.) without coupling the core to vendors, see **`06-identity-and-external-policy.md`**.

---

## 1. Access model

### 1.1 Principals

A principal is an identity that accesses memory. Principals are **opaque strings** - Astrocytes does not manage user databases or authentication. It receives a principal identifier from the caller and enforces policies against it.

```python
# Principal comes from caller context
brain = Astrocyte.from_config("astrocytes.yaml")
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
| `forget` | `forget()` memories from the bank |
| `admin` | Bank configuration, export, import, legal hold, delete bank |

Permissions are **additive** - a principal's effective permissions are the union of all grants.

### 1.3 Grant structure

```python
@dataclass
class AccessGrant:
    bank_id: str                         # Bank this grant applies to (or "*" for all)
    principal: str                       # Who gets access (or "*" for all)
    permissions: list[str]               # ["read", "write", "forget", "admin"]
```

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

Bank templates (see `14-multi-bank-orchestration.md`) can define access patterns:

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

Access control is enforced in the **Astrocytes core**, after policy checks but before any provider or pipeline call:

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

For multi-bank operations (see `14-multi-bank-orchestration.md`), the caller must have appropriate permissions on **every** bank in the query:

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

The MCP server (see `16-mcp-server.md`) derives the principal from its configuration:

```yaml
mcp:
  principal: "agent:claude-code"         # All MCP tool calls use this identity
```

Or from the MCP client's metadata if available.

---

## 4. Access control and providers

Access control is **framework-level only**. It is not delegated to providers.

- **Tier 1 (Storage)**: The pipeline calls retrieval SPIs with bank_id. The storage layer does not check permissions - Astrocytes already did.
- **Tier 2 (Memory Engine)**: The memory engine receives requests from Astrocytes. It may have its own access control (e.g., Mystique tenant isolation), but Astrocytes' access control is the outer layer.

This means switching providers does not change access policies. Policies are defined in Astrocytes config, not in provider config.

---

## 5. Programmatic access management

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
# → [AccessGrant(bank_id="user-123", principal="agent:support-bot-1", permissions=["read", "write"]), ...]

# Check access (without making a memory call)
allowed = await brain.check_access(
    bank_id="user-123",
    principal="agent:support-bot-1",
    permission="write",
)
# → True
```

---

## 6. Audit integration

All access control decisions are logged:

- **Granted access**: logged as `access.granted` audit event
- **Denied access**: logged as `access.denied` audit event with principal, bank, and permission
- **Grant/revoke changes**: logged as `access.grant_changed` audit event

These events flow through the standard audit trail (see `18-memory-lifecycle.md`) and event hooks (see `20-event-hooks.md`).

---

## 7. Relationship to authentication

Astrocytes does **not** authenticate callers. It receives a principal identifier and trusts it. Authentication is the caller's responsibility:

- **Library usage**: the calling application asserts the principal
- **MCP server**: the MCP config asserts the principal
- **HTTP service** (if wrapped in a web framework): the auth middleware asserts the principal

This separation follows the same pattern as Kubernetes RBAC (auth middleware → identity → RBAC enforcement) and avoids coupling Astrocytes to any specific auth system.

**Integrating OIDC, SAML, API keys, or commercial IdPs:** map verified credentials to a stable **principal string** (see §1.1) before constructing `AstrocyteContext`. Optional community packages may help map JWT claims or framework-specific request objects to principals; see `06-identity-and-external-policy.md` §3.

---

## 8. Optional external authorization (PDP)

The default model in this document is **declarative grants** stored in Astrocytes (config or `grant_access`). For enterprises that centralize authorization in a **Policy Decision Point** (OPA, Cerbos, SpiceDB-backed services, Permit.io, etc.), Astrocytes supports an optional **`AccessPolicyProvider`** SPI:

- Invoked at the **same enforcement point** as §3.1 (before pipeline or engine).
- Can **replace** or **chain with** config-based grants (product-defined modes - see `06-identity-and-external-policy.md` §4.2).
- Implemented in optional packages such as **`astrocytes-access-policy-opa`**, **`astrocytes-access-policy-casbin`** (in-process [Casbin](https://casbin.org/)), or similar, not in the core library.

**Authentication (IdP) examples:** [Casdoor](https://casdoor.org/), Keycloak, Auth0 - you validate credentials **outside** Astrocytes and pass a **principal**; see `06-identity-and-external-policy.md` §3.

Astrocytes remains responsible for **audit events** (§6) and for **memory-specific** permissions (`read`, `write`, `forget`, `admin`); the external system supplies **allow/deny** (and optional reasons) for those checks.

---

## 9. Summary: adoption-friendly boundaries

| Concern | Where it lives |
|---|---|
| Prove identity (login, token validation) | **Your** middleware, API gateway, or agent host |
| Stable **principal** for memory | String in `AstrocyteContext` |
| Bank-level **permissions** | Default: §2–3; optional: external PDP via §8 |
| Audit of access decisions | Astrocytes (§6), enriched with PDP metadata when used |

For full integration patterns, package naming, and entry points, see **`06-identity-and-external-policy.md`**.
