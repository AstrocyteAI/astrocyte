# DSAR and Proxied Memory — Compliance Implications

**Status:** Design note — 2026-04-18
**Relates to:** [Astrocyte Identity Spec](astrocyte_identity_spec.md),
[Memory Lifecycle](memory-lifecycle.md),
[Data Governance](data-governance.md)

## Scope

This note covers the Data Subject Access Request (DSAR) and
right-to-erasure implications of the three identity patterns defined in
the identity spec:

- **m2u** (delegated user token) — straightforward; memory lives in
  the user's bank, DSAR reaches it by bank iteration.
- **m2m** (service account, no human user) — not a DSAR concern; no
  personal data if routed correctly.
- **m2m-proxied** (service credential acting for a user) — the
  compliance-critical path. Without the `beneficiary_user_id`
  convention, this is where silent non-compliance happens.

## DSAR coverage — the correctness condition

For every data subject request (access, rectification, erasure) covering
user `U`, Astrocyte must return **every memory whose real subject is U**,
regardless of which caller retained it.

This is straightforward when every memory lives under `user-{U.oid}`.
The m2m-proxied path breaks the straightforward version because the
memory was retained under a service credential — without
`beneficiary_user_id`, the MIP routing rules have no way to know the
memory's true subject.

## How the convention closes the gap

With the `proxied-user-data` rule from
[`examples/mip-enterprise-identity.yaml`](../../astrocyte-py/examples/mip-enterprise-identity.yaml):

```yaml
- name: proxied-user-data
  priority: 20
  match:
    all:
      - principal_type: service
      - metadata.beneficiary_user_id: present
  action:
    bank: "user-{metadata.beneficiary_user_id}"
```

Memory retained under a service credential but *declared to be about*
user `U` is routed to `user-U`. A DSAR for `U` iterates `user-U` and
finds it.

Without the rule (or without the caller setting `beneficiary_user_id`),
the memory lands in `svc-{app_id}`. DSAR iteration over user banks does
not see it.

## What operators must verify

The convention is a **documented contract**, not a technical
enforcement. Astrocyte cannot infer which human a proxied call is for —
the orchestration layer is the only place that holds both the user's
delegated token and the service credential.

Operators running m2m-proxied flows must verify, at integration test
time, that:

1. **Every proxy call declares a beneficiary.** Grep integration code
   for `brain.retain(..., context=service_ctx, ...)` and confirm
   `metadata["beneficiary_user_id"]` is set. The `machine-default`
   rule's observability tag (`m2m, operational`) should never appear
   on content types that can carry personal data.
2. **A DSAR round-trip test exists.** Seed a test user's HR memory via
   the proxy flow → run DSAR for that user → assert the HR memory
   surfaces. Without this test, regressions are silent for months.
3. **Audit log includes the proxy chain.** The `proxied-user-data`
   rule's `observability_tags` surface on every routing decision;
   confirm those are captured in the retain audit log alongside
   `service_account_id` and `beneficiary_user_id`.

## Erasure — the harder half

PDPA Article 16, GDPR Article 17, and HIPAA §164.526 all grant users a
right to **erase** their personal data. For direct m2u memories this is
a straightforward `brain.forget(bank_id="user-U", compliance=True, ...)`.

For proxied memories, erasure works **only if** the routing rule used
the correct user bank at retain time. If the proxied memory landed in
`svc-{app_id}` (beneficiary missing), erasure via
`brain.forget(bank_id="user-U")` does **not** reach it.

This means the data subject's right to erasure is only satisfiable
to the extent the beneficiary convention was followed at retain time.
A missed declaration is not fixable at erasure time by iterating machine
banks — those may contain other users' data under the same service
account.

### Mitigations

1. **Make `machine-default` audit observable in real time.** Pipe
   `astrocyte.mip` structured logs to the compliance dashboard; alert
   when `machine-default` fires for content types known to carry
   personal data. Treat the alert as a P2 incident, not a warning to
   ignore.
2. **Periodic retrospective scan.** Run a scheduled job that scans
   machine banks for content that looks personal (regex, PII scanner)
   and flags records for human review + beneficiary reassignment.
3. **Contract tests at integration boundary.** The orchestration API
   (Option B from the spec) should reject proxy calls whose content
   schema indicates personal data but whose metadata lacks
   `beneficiary_user_id`.

## Legal hold vs. erasure

MAS TRM and MAS Notice 626 require financial institutions to retain
interaction records for 5–7 years. This coexists with GDPR /
PDPA erasure rights via the **tombstone** forget mode:

```yaml
forget:
  version: 1
  mode: tombstone
  audit: required
  respect_legal_hold: true
```

Personal identifiers are scrubbed (erasure obligation met) while the
structural record and audit entry remain (retention obligation met).
The `audit-strict` preset bundles these defaults.

See [`memory-lifecycle.md`](memory-lifecycle.md) for the full forget
vocabulary. The m2m-proxied path should usually use `mode: soft` or
`mode: tombstone` — hard delete of proxied data is rarely the right
choice because the service-account audit trail must remain intact even
after the user's personal data is scrubbed.

## Jurisdictional notes

| Framework | Proxied-memory obligation |
|---|---|
| PDPA (Singapore) | Personal data must be attributed. Proxy flows without `beneficiary_user_id` fail this. |
| MAS TRM | Individual-level interaction records. Machine banks do not satisfy. |
| MAS Notice 626 | Customer data handling audit trails must be customer-level. |
| IM8 (Singapore government) | Classified data must be attributed to the individual it concerns. |
| GDPR (EU) | Data subject has right of access + erasure for their data; proxy flows must make that data locatable. |
| HIPAA (US) | Patient data must be attributable and accessible for individual rights requests. |

In all cases, the same conclusion: proxy flows **require** an
orchestration-layer declaration of the beneficiary. Astrocyte provides
the routing and lifecycle primitives to act on that declaration; it
cannot fabricate the declaration itself.

## Open questions

| # | Question | Status |
|---|---|---|
| Q1 | Should Astrocyte expose a `list_proxied_banks()` helper so DSAR tooling can enumerate which service accounts retained under a given beneficiary? | Deferred — not required for the L3 core. Belongs in Mystique's eval/DSAR tooling. |
| Q2 | Should the `machine-default` rule's audit warning be a hook event that compliance platforms can subscribe to? | Likely yes — tracked as a separate item. |
| Q3 | Should we provide a CLI to audit a deployment's MIP config for "proxy rule present + catch-all has audit warning"? | Candidate for the next `astrocyte mip audit` subcommand. |

## See also

- [Astrocyte Identity Spec §3 Gap 4](astrocyte_identity_spec.md)
- [Proxied User Identity guide](../_plugins/proxied-user-identity.md)
- [Memory Lifecycle](memory-lifecycle.md) — forget modes, legal hold,
  tombstone semantics
- [ADR-005 — JWT Identity Extends Actor Model](adr/adr-005-jwt-identity-extends-actor-model.md)
