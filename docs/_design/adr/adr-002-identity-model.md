# ADR-002: Identity Model Evolution

- **Status**: Accepted (Phase 1 implemented in core)
- **Date**: 2026-04-11
- **Decision Makers**: Platform team
- **Context Area**: Domain -- Identity & Access bounded context

## Context

Astrocyte's current identity model is a single opaque string:

```python
@dataclass
class AstrocyteContext:
    principal: str  # e.g. "agent:support-bot-1", "user:calvin"
```

This was sufficient for single-agent, single-user deployments. v1.0.0 introduces requirements that the opaque string cannot support:

1. **On-Behalf-Of (OBO) delegation**: Agents act on behalf of users. When `agent:support-bot` acts for `user:calvin`, the effective permissions must be the intersection of both principals' grants. A single `principal` field cannot represent two identities simultaneously.

2. **Permission intersection**: Current permissions are additive per principal. With OBO, we need `effective = agent_grants INTERSECT user_grants` to prevent privilege escalation. This requires knowing which identity is the actor and which is the delegator.

3. **Structured claims**: External IdPs provide JWT claims and OIDC attributes (roles, groups, tenant membership). The opaque string discards this information, forcing downstream systems to re-resolve it.

4. **Multi-tenancy**: Standalone gateway deployment serves multiple tenants. Tenant isolation requires a `tenant_id` on every request context, not encoded inside the principal string.

5. **Integration migration**: All 19 framework integrations currently pass no context at all. Any identity model change must work when no context is provided (the zero-context case) and allow incremental migration.

## Decision

**Evolve `AstrocyteContext` from an opaque principal string to a structured identity context, using a three-phase backwards-compatible migration.**

### Structured AstrocyteContext

```python
@dataclass
class ActorIdentity:
    type: str   # "user" | "agent" | "service"
    id: str     # unique within type
    claims: dict[str, str] | None = None  # JWT/OIDC claims

@dataclass
class AstrocyteContext:
    # Phase 1: principal remains primary, new fields optional
    principal: str  # backwards-compatible

    actor: ActorIdentity | None = None
    on_behalf_of: ActorIdentity | None = None
    tenant_id: str | None = None
```

### Identity Resolution Logic

```
IF actor is provided:
    Use actor directly.
    principal is available for logging but not identity resolution.
ELIF principal is provided:
    Parse convention: "agent:X" -> ActorIdentity(type="agent", id="X")
                      "user:X"  -> ActorIdentity(type="user", id="X")
                      "service:X" -> ActorIdentity(type="service", id="X")
                      "X" (no prefix) -> ActorIdentity(type="user", id="X")
ELSE:
    Anonymous context. Default policy applies (owner_only -> deny, open -> allow).
```

### Permission Evaluation

```
effective_permissions(context, bank_id) =
    actor_grants = grants_for(context.actor, bank_id)
    IF context.on_behalf_of:
        obo_grants = grants_for(context.on_behalf_of, bank_id)
        RETURN actor_grants INTERSECT obo_grants
    ELSE:
        RETURN actor_grants
```

### Migration Phases

| Phase | Version | Change | Breaking? |
|---|---|---|---|
| 1 | v0.5.0 (M1) / targets v1.0.0 | Add optional `actor`, `on_behalf_of`, `tenant_id` to `AstrocyteContext`. `principal` remains the primary field. Internal code uses `actor` if present, falls back to parsing `principal`. OBO = permission **intersection** per bank in ACL. | No |
| 2 | v1.1.0 | Integrations updated to pass structured context. `principal` becomes computed from `actor` when `actor` is set. Deprecation warning emitted when `actor` is None but `principal` is set. | No |
| 3 | v2.0.0 | `principal` field removed. `actor` becomes required. `AstrocyteContext(actor=...)` is the only constructor. | Yes |

### Implementation status (Phase 1)

Shipped in `astrocyte` Python core:

- Types: `ActorIdentity`, extended `AstrocyteContext` (`actor`, `on_behalf_of`, `tenant_id`, optional `claims` on `ActorIdentity`).
- ACL: `effective_permissions` with OBO intersection; `BankResolver` + optional `identity.auto_resolve_banks` for recall/reflect when no `bank_id` / `banks` are passed (see ADR-003 `identity` subsection for config keys).
- **Not** in core yet: validating JWT/OIDC or driving policy from `claims`; multi-tenant enforcement using `tenant_id` (field reserved for callers and future gateway work). Integration adapters still migrate incrementally (e.g. optional `context` on selected integrations).

## Alternatives Considered

### Alternative A: Replace `principal` immediately

Remove `principal: str`, make `actor: ActorIdentity` required in v1.0.0.

**Rejected because**: Breaks all 19 integrations and every existing user simultaneously. The migration burden is too high for a v1.0.0 release. Users who just `pip install astrocyte` and pass `principal="user:me"` would get immediate TypeError.

### Alternative B: Separate `IdentityContext` object

Create a new `IdentityContext` class and pass it alongside `AstrocyteContext` as a separate parameter on every API method.

**Rejected because**: Doubles the number of parameters on every API method (`retain(request, context, identity)`). The identity IS part of the request context -- separating them creates an artificial boundary. Every call site must remember to pass both objects.

### Alternative C: Keep opaque strings, encode OBO in string format

Use `principal="agent:support-bot|obo:user:calvin"` -- encode delegation in the string.

**Rejected because**: Requires custom parsing at every consumption point. No type safety. Easy to construct malformed strings. Claims (JWT attributes) cannot be represented. This is primitive obsession elevated to an architecture anti-pattern.

## Consequences

### Positive

- OBO delegation enables secure agent-on-behalf-of-user workflows without privilege escalation.
- Structured claims enable fine-grained policy decisions (role-based, group-based, attribute-based) without re-resolving against the IdP (consumption in core policy is **future**; `claims` is carried on `ActorIdentity` for adapters).
- Three-phase migration ensures no breaking change for Phase 1 adopters.
- `tenant_id` field unblocks standalone gateway multi-tenancy.

### Negative

- `AstrocyteContext` grows from 1 field to 4. Complexity increases.
- Integration adapters must be updated (19 adapters, Phase 2). This is labor-intensive but can be parallelized.
- Two code paths coexist during Phase 1-2: "parse principal" and "use actor directly". Unit tests must cover both paths.

### Risks

- **Risk**: Integration authors (community contributors) may not update their adapters promptly, extending the Phase 1 period indefinitely. **Mitigation**: Provide a helper function `AstrocyteContext.from_principal(s)` that handles parsing, making the structured path easy to adopt. Emit deprecation warnings in Phase 2 to create urgency.
- **Risk**: Permission intersection logic is more complex than additive grants. Bugs in intersection could either over-permit (security vulnerability) or under-permit (broken functionality). **Mitigation**: Comprehensive Given/When/Then test suite for permission evaluation covering all combinations: actor-only, OBO with overlapping grants, OBO with disjoint grants, wildcard grants, no grants.
