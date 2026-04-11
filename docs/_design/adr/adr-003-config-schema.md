# ADR-003: Config Schema Evolution

- **Status**: Proposed
- **Implementation target**: **v0.5.0**, same release tag as ADR-002 (M1); milestones M1 and M2 are compressed into one minor.
- **Date**: 2026-04-11
- **Decision Makers**: Platform team
- **Context Area**: Application -- Configuration architecture

## Context

Astrocyte's current `AstrocyteConfig` dataclass (in `astrocyte/config.py`) covers internal framework concerns: provider selection, policy tuning, pipeline features, MCP settings, per-bank overrides, and access grants. This was sufficient when Astrocyte operated as a single-agent embedded library with no external data sources and no identity structure beyond an opaque `principal` string.

v1.0.0 introduces three capabilities that require config schema extensions:

1. **External data sources** (webhooks, event streams, API pollers) need connection definitions, authentication, extraction profiles, and scheduling -- none of which exist in the current schema.
2. **Agent registration** with explicit bank mappings and per-agent rate limits -- currently agents are implicit (whoever calls the API is the agent).
3. **Deployment mode** configuration (library vs standalone gateway) -- currently the deployment model is implicit based on how the user imports Astrocyte.

These gaps cannot be addressed by extending existing config sections. They represent new configuration domains.

### Constraints

- **Backwards compatibility is mandatory**: Existing `astrocyte.yml` files must continue to work without modification. All new sections must be optional with sensible defaults.
- **Composition with MIP**: The `sources:` section defines data source connections; MIP rules define routing of extracted content to banks. These must compose, not overlap. A source's `extraction_profile` defines HOW content is extracted; MIP defines WHERE extracted memories are routed.
- **Config-as-code**: All configuration remains declarative YAML. No imperative registration APIs for sources or agents -- config is the single source of truth.
- **Library mode must work**: All new config sections must have valid defaults for library mode. A user who only runs `Astrocyte.from_config("astrocyte.yml")` with no `sources:`, `agents:`, or `deployment:` sections gets the same behavior as today.

## Decision

**Add four optional top-level sections to `AstrocyteConfig`: `sources`, `agents`, `deployment`, and `identity`. Implement as additive dataclasses with `None` defaults. Existing config loading code requires minimal change -- new sections are parsed only if present.**

### Section 1: `sources`

Defines external data source connections. Each source has a type (webhook, event_stream, api_poll), connection details, authentication, and an extraction profile reference.

```yaml
sources:
  crm_webhooks:
    type: webhook
    path: /ingest/crm
    auth:
      method: bearer_token
      token_env: CRM_WEBHOOK_TOKEN
    extraction_profile: crm_contact
    target_bank: customer_data
    principal: service:crm-integration

  slack_events:
    type: event_stream
    driver: kafka
    topic: slack-messages
    consumer_group: astrocyte-ingest
    extraction_profile: conversation
    target_bank: team_conversations
    principal: service:slack-integration

  jira_sync:
    type: api_poll
    url: https://jira.example.com/rest/api/3/search
    interval_seconds: 300
    auth:
      method: oauth2
      token_url_env: JIRA_TOKEN_URL
      client_id_env: JIRA_CLIENT_ID
      client_secret_env: JIRA_CLIENT_SECRET
    extraction_profile: ticket
    target_bank: project_tickets
    principal: service:jira-integration

  # M4.1 — federated recall (outbound HTTP merged with local RRF; no ingest)
  remote_kb:
    type: proxy
    target_bank: shared_notes
    url: https://search.example.com/v1/query          # GET: use {query} in URL
    recall_method: POST                               # optional; default GET
    recall_body:                                      # optional JSON for POST (omit for default {query, bank_id})
      q: "__astrocyte.query__"                        # resolved to query string
      bank: "__astrocyte.bank_id__"                   # resolved to bank_id
      top_k: 8
    auth:
      type: bearer
      token: ${REMOTE_SEARCH_TOKEN}
      # or: type: api_key — header: X-API-Key, value: …
      # or: type: oauth2_client_credentials — token_url, client_id, client_secret, optional scope (RFC 6749)
      # or: type: oauth2_refresh — refresh_token + same token_url/client_*; supports refresh_token rotation in-memory
      # or: oauth2 + grant_type: refresh_token (alias for oauth2_refresh)
      # token_endpoint_auth_method: client_secret_post (default) | client_secret_basic (HTTP Basic at token URL)
      # One-time code→tokens (browser redirect): use library ``exchange_oauth2_authorization_code`` then persist refresh_token
      # or: headers: { X-Custom: "…" } merged after bearer / api_key / OAuth
```

**Design decisions within `sources`**:
- `target_bank` is the default bank for extracted memories. MIP rules can override this per-memory based on content/metadata matching.
- `principal` assigns an identity to the source for access control. The source can only write to banks where this principal has `write` grants.
- `auth` uses `_env` suffixes for secrets, resolved via existing env var substitution (`${VAR_NAME}` pattern already in `config.py`).
- `extraction_profile` references a profile name. Profile definitions are co-located in a new `extraction_profiles` section (below).
- **`type: proxy`** (M4.1) is recall-only: `url`, `target_bank`, optional `recall_method` / `recall_body`, and `auth` for outbound calls. It does not use `extraction_profile` (nothing is ingested). Response JSON uses a `hits` or `results` array of `{ text, score?, memory_id?, … }`. With **`observability.prometheus_enabled`**, proxy calls emit `astrocyte_proxy_recall_total{source_id,status}` and `astrocyte_proxy_recall_duration_seconds{source_id}`.
- **OAuth (proxy `auth`) — intentional scope**: the library implements client-credentials, refresh (with rotation), authorization-code **exchange**, and HTTP Basic at the token endpoint; callers own redirect UX and token persistence. The following can be **layered on later** when needed: a **hosted redirect / callback server** (or deeper gateway integration) for the full authorization-code UX end-to-end, **PKCE**, and **device** / **JWT bearer** token grants (RFC 6749 / OIDC extensions).

### Section 2: `agents`

Declares known agents with explicit bank mappings and per-agent rate limits.

```yaml
agents:
  support-bot:
    banks: [customer_memories, kb_articles]
    permissions: [read, write]
    max_retain_per_minute: 60
    max_recall_per_minute: 120

  analyst-bot:
    banks: [analytics_data, customer_memories]
    permissions: [read]
    max_recall_per_minute: 200
```

**Design decisions within `agents`**:
- Agent definitions generate `AccessGrant` entries at config load time, merged with existing `access_grants`. This means agent-level grants compose with bank-level grants from `banks.*.access` -- no new permission resolution path needed.
- Per-agent rate limits override global `homeostasis.rate_limits` for the specific agent principal (`agent:{agent_id}`).
- Unknown agents (not listed in config) fall back to global rate limits and require explicit `access_grants` entries. This preserves backwards compatibility -- existing deployments without an `agents:` section work unchanged.

### Section 3: `deployment`

Configures deployment mode. Only relevant for standalone gateway mode -- library mode ignores this section entirely.

```yaml
deployment:
  mode: library  # "library" | "standalone"
  # Fields below only apply when mode = "standalone"
  host: 0.0.0.0
  port: 8420
  workers: 4
  cors_origins: ["https://app.example.com"]
  tls:
    cert_path: /etc/ssl/certs/astrocyte.pem
    key_path: /etc/ssl/private/astrocyte.key
```

**Design decisions within `deployment`**:
- `mode` defaults to `"library"`. When `"standalone"`, Astrocyte expects to be run via `astrocyte serve` CLI command which starts FastAPI/Uvicorn.
- Gateway plugin mode (`"plugin"`) is deferred per ADR-001. The config section reserves the value but does not implement it.
- `workers` controls Uvicorn worker count. Default 1 for development, recommend `2 * CPU + 1` for production.

### Section 4: `identity`

Configures identity-related behavior. Complements [ADR-002](./adr-002-identity-model.md) structured `AstrocyteContext`. This section has **two layers**: keys that exist in `AstrocyteConfig.identity` today (bank resolution + ACL helpers), and **planned** gateway/JWT fields not wired in the OSS core yet.

**Implemented today** (`astrocyte.config.IdentityConfig` — optional YAML `identity:`):

```yaml
identity:
  auto_resolve_banks: false   # When true, recall/reflect may omit bank_id/banks; banks are derived from ACL + conventions
  user_bank_prefix: "user-"   # BankResolver default bank id for type user (user-{id})
  agent_bank_prefix: "agent-"
  service_bank_prefix: "service-"
```

- `auto_resolve_banks`: when enabled and `AstrocyteContext` is provided, `recall` / `reflect` resolve readable banks from `access_grants`, known `banks:` keys, and (if needed) convention banks from prefixes — see ADR-002 implementation notes.
- Prefixes adjust the convention used when suggesting a default bank for an `ActorIdentity` (e.g. `user-calvin` vs a custom prefix).

**Planned (not implemented in core config yet)** — reserved for standalone gateway / richer IdP integration:

```yaml
# Future — do not assume these keys load in current astrocyte builds
identity:
  default_actor_type: user
  allow_anonymous: false
  trusted_issuers:
    - issuer: https://auth.example.com
      audience: astrocyte
      jwks_uri: https://auth.example.com/.well-known/jwks.json
```

- `default_actor_type`, `allow_anonymous`, `trusted_issuers`: design intent remains as previously described; implementation will be reconciled when gateway identity lands.

### Section 5: `extraction_profiles` (companion to `sources`)

```yaml
extraction_profiles:
  crm_contact:
    content_type: json
    chunking_strategy: semantic
    entity_extraction: true
    metadata_mapping:
      $.name: contact_name
      $.email: contact_email
    tag_rules:
      - match: "$.type == 'lead'"
        tags: [lead, prospect]

  conversation:
    content_type: text
    chunking_strategy: paragraph
    entity_extraction: true

  ticket:
    content_type: json
    chunking_strategy: fixed_size
    chunk_size: 1000
    entity_extraction: true
    metadata_mapping:
      $.key: ticket_id
      $.fields.summary: title
      $.fields.status.name: status
```

## Alternatives Considered

### Alternative 1: Separate config files per concern

Split into `astrocyte.yml` (core), `sources.yml` (ingestion), `agents.yml` (identity). Each file loaded independently.

- **Pro**: Clean separation, smaller files, each concern independently versionable.
- **Con**: Config composition becomes complex (cross-file references between sources and banks, agents and access grants). Users must manage multiple files. Existing `AstrocyteConfig.from_config()` API would need signature change. Violates backwards compatibility constraint.
- **Rejected because**: The composition overhead exceeds the organizational benefit. A single `astrocyte.yml` with optional sections is simpler and preserves the current loading API.

### Alternative 2: Programmatic registration only

No YAML for sources/agents. Users register via Python API: `brain.register_source(...)`, `brain.register_agent(...)`.

- **Pro**: Type-safe at registration time, no YAML parsing ambiguity, IDE autocomplete.
- **Con**: Not declarative -- cannot be version-controlled as config. Breaks "config-as-code" principle. Cannot be used with standalone gateway (no Python code to call). Requires code changes to modify sources/agents.
- **Rejected because**: Declarative config is a core principle. Programmatic registration may be offered as a supplementary API but cannot be the primary mechanism.

### Alternative 3: Extend existing sections instead of adding new ones

Put source definitions under `banks.*.sources`, agent settings under `access_grants`, deployment under a `server` key in the root.

- **Pro**: Fewer top-level sections, conceptually linked to existing structures.
- **Con**: Sources are not a property of banks (a source can target multiple banks via MIP routing). Agent settings are more than access grants (they include rate limits, bank mappings). Overloading existing sections creates confusing semantics.
- **Rejected because**: Sources, agents, and deployment are orthogonal configuration domains that deserve their own top-level sections.

## Consequences

### Positive
- Backwards compatible -- existing configs work unchanged (all new sections default to `None`)
- Single file remains the config source of truth
- Declarative, version-controllable configuration for all v1.0.0 features
- Extraction profiles compose with MIP routing (profiles define extraction, MIP defines routing targets)
- Agent registration auto-generates access grants, reducing config duplication

### Negative
- `AstrocyteConfig` dataclass grows from ~20 fields to ~25 fields. Mitigated by grouping into sub-dataclasses (`SourcesConfig`, `AgentsConfig`, `DeploymentConfig`, `IdentityConfig`).
- YAML validation surface increases. Mitigated by adding JSON Schema generation from dataclass definitions for editor support.
- Source auth secrets via env var indirection (`_env` suffix) is a convention that must be documented clearly. Users unfamiliar with the pattern may hardcode secrets in YAML.

### Migration
- No migration needed. Existing configs are valid. New sections are additive.
- Users upgrading to v1.0.0 can add sections incrementally as they adopt new features.
