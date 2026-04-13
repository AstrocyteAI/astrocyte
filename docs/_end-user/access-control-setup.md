# Access control setup

Practical guide to configuring principals, grants, and per-bank permissions. Authentication identifies *who* is calling — access control decides *what* they can do.

---

## Enable access control

Access control is **disabled by default**. When disabled, all operations succeed regardless of context.

```yaml
access_control:
  enabled: true
  default_policy: owner_only       # "owner_only" | "open" | "deny"
```

| Policy | No context (anonymous) | Context with no matching grant |
|--------|----------------------|-------------------------------|
| `owner_only` | Denied | Denied |
| `open` | Allowed | Allowed |
| `deny` | Denied | Denied |

**Recommendation:** Use `deny` for production (explicit grants only) or `owner_only` for multi-tenant setups where users own their banks.

---

## Permissions

| Permission | Operations allowed |
|------------|-------------------|
| `read` | `recall()`, `reflect()` |
| `write` | `retain()` |
| `forget` | `forget()` with specific `memory_ids` or `tags` |
| `admin` | `forget(scope="all")`, bank config, export/import |
| `*` | All of the above |

---

## Define grants

### Top-level grants

Apply across the entire instance. Use `bank_id: "*"` for all banks, or a specific bank ID.

```yaml
access_grants:
  # Alice has full control of her bank
  - bank_id: "user-alice"
    principal: "user:alice"
    permissions: [read, write, forget, admin]

  # Support bot can read and write to any shared bank
  - bank_id: "shared-*"
    principal: "agent:support-bot"
    permissions: [read, write]

  # Everyone can read the public bank
  - bank_id: "public"
    principal: "*"
    permissions: [read]

  # Admin has full access everywhere
  - bank_id: "*"
    principal: "user:admin"
    permissions: ["*"]
```

### Per-bank grants

Define access directly on a bank — useful for bank-specific overrides:

```yaml
banks:
  team-engineering:
    access:
      - principal: "user:*"
        permissions: [read, write]
      - principal: "agent:code-reviewer"
        permissions: [read]

  sensitive-data:
    access:
      - principal: "user:compliance-officer"
        permissions: [read, forget, admin]
```

Top-level `access_grants` and per-bank `banks.*.access` are **merged** — both contribute grants. There's no precedence; all grants are unioned together.

### Agent registration grants

Register agents with bank access and optional rate hints. This generates implicit grants:

```yaml
agents:
  ingester:
    principal: "agent:ingester"
    banks: [raw-data, processed-data]
    permissions: [write]

  analyst:
    principal: "agent:analyst"
    banks: [processed-data, reports]
    permissions: [read]
    default_bank: processed-data

  admin-bot:
    principal: "agent:admin-bot"
    banks: ["*"]                    # all banks
    permissions: [read, write, forget, admin]
    max_retain_per_minute: 120
    max_recall_per_minute: 240
```

If `permissions` is omitted, agents default to `[read, write]`.

---

## Principal format

Principals follow the `type:id` format:

| Type | Example | Use for |
|------|---------|---------|
| `user:` | `user:alice`, `user:u-12345` | Human users |
| `agent:` | `agent:support-bot`, `agent:ingester` | AI agents and automated services |
| `service:` | `service:etl-worker` | Backend services |
| `*` | `*` | Any principal (wildcard) |

In `dev` and `api_key` auth modes, the client sets the principal via the `X-Astrocyte-Principal` header. In `jwt_oidc` mode, the principal is computed from JWT claims. See [authentication setup](authentication-setup/).

---

## How grants are evaluated

When an operation is called (e.g. `brain.retain(content, bank_id="b1", context=ctx)`):

1. If `access_control.enabled` is `false` → **allow** (skip all checks)
2. If context is `None` (anonymous):
   - `open` policy → **allow**
   - `owner_only` or `deny` → **deny** (403)
3. If context is provided:
   - Collect all grants where `bank_id` matches (`"*"` or exact) **and** `principal` matches (`"*"` or exact)
   - Union all matching permissions
   - If the required permission is in the set → **allow**
   - If not, and policy is `open` → **allow**
   - Otherwise → **deny** (403)

### Example: grant evaluation

```yaml
access_grants:
  - bank_id: "*"
    principal: "user:alice"
    permissions: [read]
  - bank_id: "user-alice"
    principal: "user:alice"
    permissions: [read, write, forget, admin]
```

| Operation | Bank | Result | Why |
|-----------|------|--------|-----|
| `recall()` | `user-alice` | Allowed | `read` from both grants |
| `retain()` | `user-alice` | Allowed | `write` from bank-specific grant |
| `retain()` | `other-bank` | Denied | Only `read` from wildcard grant |
| `recall()` | `other-bank` | Allowed | `read` from wildcard grant |
| `forget(scope="all")` | `user-alice` | Allowed | `admin` from bank-specific grant |
| `forget(scope="all")` | `other-bank` | Denied | No `admin` on wildcard grant |

---

## On-behalf-of (OBO)

When an agent acts on behalf of a user, effective permissions are the **intersection** of both principals' grants. This prevents privilege escalation.

Enable OBO:

```yaml
identity:
  obo_enabled: true
```

### Example

```yaml
access_grants:
  - bank_id: "shared"
    principal: "agent:support-bot"
    permissions: [read, write, forget]
  - bank_id: "shared"
    principal: "user:alice"
    permissions: [read]
```

When `support-bot` acts on behalf of `alice`:

```python
ctx = AstrocyteContext(
    actor=ActorIdentity(type="agent", id="support-bot"),
    on_behalf_of=ActorIdentity(type="user", id="alice"),
)

# Agent permissions: {read, write, forget}
# Alice permissions: {read}
# Intersection: {read}

await brain.recall("query", bank_id="shared", context=ctx)   # Allowed (read)
await brain.retain("data", bank_id="shared", context=ctx)     # Denied (write not in intersection)
```

The agent cannot write on behalf of Alice because Alice only has `read` permission — even though the agent itself has `write`.

---

## Identity-driven bank resolution

Automatically resolve bank IDs from the caller's identity:

```yaml
identity:
  auto_resolve_banks: true
  user_bank_prefix: "user-"
  agent_bank_prefix: "agent-"
  service_bank_prefix: "service-"
  resolver: convention
```

With convention-based resolution, a user with `actor.id = "alice"` gets a default bank of `user-alice`. An agent with `actor.id = "ingester"` gets `agent-ingester`.

---

## Error responses

When access is denied, the gateway returns **403 Forbidden**:

```json
{
  "detail": "Principal 'agent:bot' denied 'write' on bank 'bank-1'"
}
```

The error message includes the principal, the denied permission, and the bank — useful for debugging grant configuration.

---

## Common patterns

### Personal banks (multi-tenant)

Each user owns their own bank. No cross-access without explicit grants.

```yaml
access_control:
  enabled: true
  default_policy: owner_only

identity:
  auto_resolve_banks: true
  resolver: convention

access_grants:
  # Users get full access to their own bank (convention: user-{id})
  - bank_id: "*"
    principal: "*"
    permissions: [read, write, forget]
```

With `owner_only` policy, this only grants access when the bank ID matches the principal's convention bank. A user `user:alice` can access `user-alice` but not `user-bob`.

### Shared team bank

A team shares a bank; individual agents have limited access.

```yaml
access_grants:
  # All team members can read and write
  - bank_id: "team-engineering"
    principal: "user:*"
    permissions: [read, write]

  # Only the lead can delete
  - bank_id: "team-engineering"
    principal: "user:team-lead"
    permissions: [read, write, forget, admin]

  # CI bot can write but not read
  - bank_id: "team-engineering"
    principal: "agent:ci-bot"
    permissions: [write]
```

### Read-only analytics

An analytics agent can read from multiple banks but never write or delete.

```yaml
agents:
  analytics:
    principal: "agent:analytics"
    banks: [user-*, team-*, shared-*]
    permissions: [read]
```

### Compliance lockdown

A compliance officer can read and purge any bank. Regular users cannot forget.

```yaml
access_control:
  enabled: true
  default_policy: deny

access_grants:
  - bank_id: "*"
    principal: "user:compliance-officer"
    permissions: [read, forget, admin]

  - bank_id: "*"
    principal: "user:*"
    permissions: [read, write]
```

---

## Testing your grants

### Verify with curl

```bash
# This should succeed (alice has read on her bank)
curl -X POST http://localhost:8080/v1/recall \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: $API_KEY" \
  -H "X-Astrocyte-Principal: user:alice" \
  -d '{"query": "preferences", "bank_id": "user-alice"}'

# This should return 403 (alice has no write on other-bank)
curl -X POST http://localhost:8080/v1/retain \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: $API_KEY" \
  -H "X-Astrocyte-Principal: user:alice" \
  -d '{"content": "test", "bank_id": "other-bank"}'
```

### Verify in Python

```python
from astrocyte import Astrocyte
from astrocyte.types import AstrocyteContext, ActorIdentity
from astrocyte.errors import AccessDenied

brain = Astrocyte.from_config("astrocyte.yaml")

ctx = AstrocyteContext(
    principal="user:alice",
    actor=ActorIdentity(type="user", id="alice"),
)

# Should succeed
await brain.recall("test", bank_id="user-alice", context=ctx)

# Should raise AccessDenied
try:
    await brain.retain("test", bank_id="other-bank", context=ctx)
except AccessDenied as e:
    print(f"Denied: {e}")  # "Principal 'user:alice' denied 'write' on bank 'other-bank'"
```

---

## Further reading

- [Authentication setup](authentication-setup/) — how identity reaches the gateway
- [Configuration reference](configuration-reference/) — `access_control`, `identity`, `access_grants` config schema
- [Access control](../design/access-control/) — design doc with permission model and architecture
- [ADR-002 Identity model](../design/adr/adr-002-identity-model/) — structured identity design rationale
