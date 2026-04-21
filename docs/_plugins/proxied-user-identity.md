---
title: Proxied User Identity (beneficiary_user_id)
description: The documented convention for declaring the beneficiary user when an agent acts on their behalf via a service account credential.
---

# Proxied User Identity

Some enterprise systems authenticate machine-to-machine only. They accept
a service account token but refuse delegated user tokens — regardless of
what the calling agent holds. HR systems, ITSM platforms, finance
backends, and many older internal APIs fit this pattern.

The data these systems return is often **personal** (leave balances,
support tickets about a specific person, expense reports). If Astrocyte
stores that data under the service account's machine bank, it is
effectively **orphaned** — the user who owns the data will not find it
via recall, it will not surface on a DSAR, and right-to-erasure cannot
reach it.

The `beneficiary_user_id` convention exists for exactly this case.

## The contract

When an agent calls `brain.retain(...)` via a service credential and
the data concerns a specific human, the **calling orchestration layer**
must set `metadata.beneficiary_user_id` to that user's stable subject
identifier (the `oid` / `sub` from the delegated token the orchestration
layer holds).

```python
await brain.retain(
    content="Leave balance: 5 days remaining, expires 2026-12-31",
    content_type="hr_record",
    # Do NOT set bank_id — let MIP route based on beneficiary.
    metadata={
        # REQUIRED when acting on behalf of a user via service credential.
        "beneficiary_user_id": user_subject_id,  # oid/sub from user token

        # RECOMMENDED — helps MIP apply source-specific rules.
        "source_system": "hr-system",

        # RECOMMENDED — documents why a user token was not available.
        "proxy_reason": "Downstream system does not support delegated tokens",

        # OPTIONAL — links back to the triggering session.
        "originating_session_id": session_id,
    },
    context=service_account_context,
)
```

## What happens when `beneficiary_user_id` is declared

The `proxied-user-data` rule in
[`examples/mip-enterprise-identity.yaml`](../../astrocyte-py/examples/mip-enterprise-identity.yaml)
fires. The memory is routed into the **user's** bank, not the machine
bank. Full user-lifecycle applies:

- Recall queries from the user's agent surface this memory.
- DSAR for the user includes it.
- Right-to-erasure reaches it (soft-delete or hard-delete per rule config).
- Audit trail records the proxy chain (service account ID + declared
  beneficiary).
- PII scanning is mandatory — these systems typically return personal data.

## What happens when `beneficiary_user_id` is absent

The `machine-default` rule fires. Data lands in `svc-<app_id>`. It is
**not** user-attributed. **This is intentional** — Astrocyte will not
guess which human a proxied call is for. The calling orchestration
layer is the only party that holds both the user's delegated token and
the service credential; it is the only layer that can make the
declaration truthfully.

Operators who want visibility into silent proxy calls should consume the
`astrocyte.mip` structured log stream and alert when the
`machine-default` rule fires for content types that typically carry
personal data.

## Why not infer the beneficiary automatically?

The spec rejects automatic inference for three reasons:

1. **Wrong layer.** Astrocyte sees one request at a time. The
   orchestration layer sees the delegated token and the service-credential
   call as two halves of one logical operation. Inference from Astrocyte's
   vantage point would be unreliable.
2. **Silent errors are the compliance hazard.** If Astrocyte guessed and
   got the user wrong, it would retain Alice's HR data in Carol's bank
   — and the error would be invisible until the next DSAR surfaced it.
   An explicit declaration converts that into a loud test-time failure.
3. **Different enterprises normalize user IDs differently.** The
   downstream HR system's `employee_id` is usually not the same as the
   IdP's `oid`. The orchestration layer owns the mapping.

## Integration pattern — custom orchestration API (Option B)

```python
async def get_hr_data_on_behalf_of_user(delegated_token: str) -> Any:
    # 1. Extract the user's stable identifier from the delegated token
    #    (the orchestration layer already did signature validation).
    user_claims = decode_jwt(delegated_token)
    user_subject_id = user_claims["oid"]  # or "sub" for generic OIDC

    # 2. Call the downstream HR system with the service credential
    #    (HR system does not accept delegated tokens).
    result = await hr_client.fetch_leave_balance(
        employee_id=map_oid_to_employee_id(user_subject_id),
    )

    # 3. Retain into Astrocyte with explicit beneficiary declaration.
    #    Astrocyte is called with the SERVICE credential, but MIP routes
    #    the memory into the USER's bank because beneficiary is set.
    await brain.retain(
        content=format_result(result),
        content_type="hr_record",
        metadata={
            "beneficiary_user_id": user_subject_id,
            "source_system": "hr",
            "proxy_reason": "HR system does not support delegated tokens",
        },
        context=service_account_context,
    )

    return result
```

## Compliance implications

Per-user attribution is not an architectural preference — it is a
regulatory requirement in every jurisdiction the identity spec
enumerates (PDPA, MAS TRM/626, IM8, GDPR, HIPAA). Deploying m2m-proxied
flows **without** the `beneficiary_user_id` convention creates direct
exposure:

- **PDPA / GDPR**: data subject access and erasure cannot be fulfilled
  for data held under a machine bank that the user never appeared in.
- **MAS TRM**: financial institutions must maintain individual-level
  audit trails — silent machine-bank routing breaks that.
- **IM8**: classified Singapore government data must be attributed to
  the individual it concerns.
- **HIPAA**: patient data must be accessible for individual rights
  requests.

## See also

- [Astrocyte Identity Spec §3 Gap 4](../_design/astrocyte_identity_spec.md)
- [JWT Identity Middleware](jwt-identity-middleware.md) — the m2u / m2m
  half of the identity story (resolves incoming tokens into an
  `ActorIdentity`)
- [`examples/mip-enterprise-identity.yaml`](../../astrocyte-py/examples/mip-enterprise-identity.yaml)
  — reference MIP config including the proxy rule and machine-default
  catch-all
