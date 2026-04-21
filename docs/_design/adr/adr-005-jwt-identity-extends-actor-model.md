# ADR-005 — JWT Identity Classifier extends ActorIdentity, not a parallel model

**Status:** Accepted — 2026-04-18
**Relates to:** [ADR-002 Identity Model](adr-002-identity-model.md),
[Astrocyte Identity Spec](../astrocyte_identity_spec.md)

## Context

The [Astrocyte Identity Spec](../astrocyte_identity_spec.md) §3 Gap 1
proposes a new `Identity` dataclass with fields `type ∈ {user,
service_principal, anonymous}`, `oid`, `upn`, `app_id`, `tenant_id`,
`raw_claims` — and a `JWTIdentityMiddleware` that populates it on every
MCP request.

An earlier ADR-002 already established the `ActorIdentity` dataclass
used for access control and bank resolution:

```python
@dataclass
class ActorIdentity:
    type: str  # "user" | "agent" | "service"
    id: str
    claims: dict[str, str] | None = None
```

The spec's proposal and the existing model substantially overlap. This
ADR records which model the identity-from-JWT work extends, and why.

## Decision

The JWT classifier **extends** the existing `ActorIdentity` model. No
parallel dataclass is introduced.

Specifically:

- `ActorIdentity.type` stays the same discriminator. `"user"` matches
  the spec's `"user"`. The spec's `"service_principal"` is spelled
  `"service"` here — same semantic, legacy spelling preserved.
- The spec's new fields (`oid`, `upn`, `app_id`, `tenant_id`) are
  stashed into `ActorIdentity.claims` under well-known keys:
  `claims["upn"]`, `claims["app_id"]`, `claims["tenant_id"]`, and
  `claims["idtyp"]` when present.
- The spec's `raw_claims: dict` field is not added — callers should
  log the raw token payload at the MCP transport layer if they need it
  for audit, not persist it on the identity object.
- Bank-prefix naming (`user-`, `service-`, `svc-`) is a **separate**
  concern handled by `derive_bank_id` and `BankResolver`. Same
  `ActorIdentity` can produce different bank IDs depending on the
  deployment's prefix convention.

## Alternatives considered

### Alternative A — Spec as written (parallel `Identity` class)

**Rejected.** Would duplicate the identity concept across the codebase.
The policy layer (`astrocyte.policy`) and access control already
consume `ActorIdentity` — splitting into a second `Identity` type would
either require conversion adapters at every boundary or force a
migration of the entire access-control subsystem.

### Alternative B — Extend `ActorIdentity` with first-class `oid` / `upn` / `app_id` fields

**Rejected for this iteration.** Adding fields to the dataclass is a
breaking change for any downstream code that constructs
`ActorIdentity(type=..., id=..., claims=...)` positionally or via
typed init. The `claims` dict already accepts string-keyed arbitrary
attribution data; stashing there is additive.

If operator ergonomics demand first-class accessors later (e.g.
`identity.upn` instead of `identity.claims["upn"]`), we can add them as
`@property` readers over `claims` without a schema break.

### Alternative C — Skip the new code entirely, require callers to populate `ActorIdentity` themselves

**Rejected.** The classification rules (which claim signals a user
token vs a service credential) are load-bearing for compliance and
exactly the kind of thing that must not be reimplemented at every
integration point. Centralising in
`astrocyte.identity_jwt.classify_jwt_claims` is the whole point.

## Consequences

- **Behavioral:** None for existing code paths. `ActorIdentity` objects
  built by the existing `parse_principal` / `resolve_actor` helpers are
  unchanged.
- **New surface:** `astrocyte.identity_jwt` module with
  `classify_jwt_claims` and `derive_bank_id`. Pure functions, no
  network I/O, no JWT library dependency.
- **MIP surface:** `RuleEngineInput.actor_identity` field; new match
  fields `principal_type` / `principal_id` / `principal_upn` /
  `principal_app_id`; new template variables `{principal.*}`.
- **Bank-prefix flexibility:** `derive_bank_id` accepts
  `service_bank_prefix` and `user_bank_prefix` kwargs so deployments
  can pick `svc-<id>` or `service-<id>` without changing the core.
- **Forward compatibility:** If a later spec revision requires
  first-class `oid` / `upn` accessors, adding them as properties over
  `claims` is non-breaking. If the `raw_claims` audit trail becomes a
  requirement, it can be added as a new optional field without touching
  `ActorIdentity.claims`.

## Mapping table — spec names ↔ implementation

| Spec (§3 Gap 1) | Implementation |
|---|---|
| `Identity` dataclass | `astrocyte.types.ActorIdentity` |
| `type="user"` | `type="user"` (unchanged) |
| `type="service_principal"` | `type="service"` |
| `type="anonymous"` | Not a classifier output — handled by transport layer before classify_jwt_claims |
| `oid` | `claims` field: `id` + `claims["upn"]` |
| `upn` | `claims["upn"]` |
| `app_id` | `id` (for service identities) + `claims["app_id"]` |
| `tenant_id` | `claims["tenant_id"]` |
| `raw_claims` | Not persisted — log at transport layer |
| `bank_id` | Derived by `derive_bank_id`, not stored on identity |
| `JWTIdentityMiddleware.resolve` | Split: `classify_jwt_claims` (pure) + transport-layer wiring (deployment-specific, in `astrocyte.mcp`) |
| `PrincipalTypeMatcher` | Implemented as extensions to `resolve_field` in `astrocyte.mip.rule_engine`; no separate matcher class — the existing match-block engine is the matcher |

## Back-propagation to the spec

The identity spec should be updated in a follow-up to reflect this
implementation:

1. Replace references to `Identity` / `service_principal` with
   `ActorIdentity` / `service`.
2. Replace the `PrincipalTypeMatcher` class description with the
   "match-field extension" framing — there is no separate matcher
   class; the rule-engine's field resolver is the extension point.
3. Clarify that bank prefix is a `derive_bank_id` kwarg, not embedded
   in the identity object.
4. Note that `raw_claims` is not persisted on `ActorIdentity`; if audit
   needs the raw payload, it must be captured at the transport layer.
