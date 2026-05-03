# Configuration reference

Complete reference for `astrocyte.yaml` — every key, type, default, and valid value.

Astrocyte loads configuration with this merge order (last wins):

1. **Compliance profile** (`compliance_profile: pdpa`) — sets barriers, lifecycle, access control, DLP
2. **Profile** (`profile: personal`) — sets defaults, homeostasis, signal quality
3. **Your `astrocyte.yaml`** — overrides everything above

All string values support `${ENV_VAR}` substitution — unresolved vars are left as-is.

---

## Provider tier & profile

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `provider_tier` | `"storage"` \| `"engine"` | `"engine"` | **Tier 1** (storage) uses Astrocyte's built-in pipeline with your own backends. **Tier 2** (engine) delegates to a full memory engine (Mystique, Mem0, Zep, etc.) |
| `profile` | string \| null | `null` | Built-in profile name (`minimal`, `personal`, `research`, `coding`, `support`) or path to custom profile file (`./my-profile.yaml`) |
| `compliance_profile` | string \| null | `null` | Pre-built compliance preset: `gdpr`, `hipaa`, or `pdpa`. Sets barriers, lifecycle, access control, and DLP automatically |
| `fallback_strategy` | `"error"` \| `"local_llm"` \| `"degrade"` | `"error"` | How to handle provider failures |

## Tier 1: Storage backends

Used when `provider_tier: storage`.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `vector_store` | string \| null | `null` | Vector store provider: `in_memory`, `pgvector`, `qdrant`, etc. |
| `vector_store_config` | dict \| null | `null` | Provider-specific settings (connection URL, dimensions, etc.) |
| `graph_store` | string \| null | `null` | Graph store provider: `neo4j`, etc. |
| `graph_store_config` | dict \| null | `null` | Provider-specific settings |
| `document_store` | string \| null | `null` | Document store provider: `elasticsearch`, etc. |
| `document_store_config` | dict \| null | `null` | Provider-specific settings |

```yaml
# Example: pgvector + Neo4j hybrid
provider_tier: storage
vector_store: postgres
vector_store_config:
  dsn: ${DATABASE_URL}
  embedding_dimensions: 1536
  bootstrap_schema: true
graph_store: neo4j
graph_store_config:
  uri: bolt://localhost:7687
  user: neo4j
  password: ${NEO4J_PASSWORD}
```

## Tier 2: Engine providers

Used when `provider_tier: engine`.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `provider` | string \| null | `null` | Engine provider name: `mystique`, `mem0`, `zep`, etc. |
| `provider_config` | dict \| null | `null` | Engine-specific settings (endpoint, API key, etc.) |

```yaml
provider_tier: engine
provider: mystique
provider_config:
  endpoint: ${MYSTIQUE_ENDPOINT}
  api_key: ${MYSTIQUE_API_KEY}
```

## LLM & embedding

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `llm_provider` | string \| null | `null` | LLM provider for reflect and extraction: `openai`, `anthropic`, `litellm`, `mock`, etc. |
| `llm_provider_config` | dict \| null | `null` | API key, model name, endpoint, etc. |
| `embedding_provider` | string \| null | `null` | Separate embedding provider (if different from LLM) |
| `embedding_provider_config` | dict \| null | `null` | Embedding-specific settings (dimensions, model, etc.) |

```yaml
llm_provider: openai
llm_provider_config:
  api_key: ${OPENAI_API_KEY}
  model: gpt-4o-mini
# The OpenAI provider handles both chat completions and embeddings.
# Use astrocyte-llm-litellm for Anthropic, Bedrock, Vertex, Ollama, and other providers.
```

---

## homeostasis

Rate limits, quotas, and token budgets.

```yaml
homeostasis:
  recall_max_tokens: 4096
  reflect_max_tokens: 8192
  retain_max_content_bytes: 51200

  rate_limits:
    retain_per_minute: 60
    recall_per_minute: 120
    reflect_per_minute: 30
    global_per_minute: null          # optional global cap

  quotas:
    retain_per_day: null             # optional daily cap
    reflect_per_day: null
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `recall_max_tokens` | int \| null | `null` | Max tokens per recall operation |
| `reflect_max_tokens` | int \| null | `null` | Max tokens per reflect operation |
| `retain_max_content_bytes` | int \| null | `null` | Max bytes per retain operation |
| `rate_limits.retain_per_minute` | int \| null | `null` | Max retain operations per minute |
| `rate_limits.recall_per_minute` | int \| null | `null` | Max recall operations per minute |
| `rate_limits.reflect_per_minute` | int \| null | `null` | Max reflect operations per minute |
| `rate_limits.global_per_minute` | int \| null | `null` | Global rate limit across all operations |
| `quotas.retain_per_day` | int \| null | `null` | Max retains per 24 hours |
| `quotas.reflect_per_day` | int \| null | `null` | Max reflects per 24 hours |

---

## barriers

Safety controls applied on every retain operation.

### barriers.pii

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `mode` | string | `"regex"` | Detection mode: `regex`, `ner`, `llm`, `rules_then_llm`, `disabled` |
| `action` | string | `"redact"` | What to do when PII is found: `redact`, `reject`, `warn` |
| `countries` | list\[string\] \| null | `null` | Country-specific patterns: `SG` (NRIC), `IN` (Aadhaar), `GB` (NINO), `US` (SSN), `AU` (TFN), `CA`, `JP`, `CN`, `DE`, `FR`, `IT`, `ES` |
| `patterns` | list\[dict\] \| null | `null` | Custom regex patterns: `[{type: "custom_id", pattern: "\\d{8}"}]` |
| `type_overrides` | dict \| null | `null` | Override action per PII type: `{credit_card: {action: reject}}` |

```yaml
barriers:
  pii:
    mode: rules_then_llm
    action: redact
    countries: [SG, IN, GB, US]
    type_overrides:
      credit_card: { action: reject }
      name: { action: warn }
```

### barriers.validation

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `max_content_length` | int | `50000` | Reject content over this many bytes |
| `reject_empty_content` | bool | `true` | Reject empty or whitespace-only content |
| `reject_binary_content` | bool | `true` | Reject non-text content |
| `allowed_content_types` | list\[string\] \| null | `null` | Optional content type whitelist |

### barriers.metadata

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `blocked_keys` | list\[string\] | `["api_key", "password", "token", "secret"]` | Keys to scrub from metadata |
| `max_metadata_size_bytes` | int | `4096` | Max metadata size in bytes |

---

## signal_quality

Deduplication and noise detection.

### signal_quality.dedup

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `true` | Enable duplicate detection |
| `similarity_threshold` | float | `0.95` | Cosine similarity threshold for duplicates (0–1) |
| `action` | string | `"skip"` | What to do with duplicates: `skip`, `warn`, `update` |

### signal_quality.noisy_bank

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `true` | Enable noisy bank detection |
| `retain_spike_multiplier` | float | `5.0` | Retain rate spike threshold |
| `min_avg_content_length` | int | `20` | Minimum average chunk length (chars) |
| `max_dedup_rate` | float | `0.8` | Max dedup rate before flagging |
| `action` | string | `"warn"` | Action on noisy bank: `warn`, `throttle`, `reject` |

---

## escalation

Circuit breaker and degraded mode.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `degraded_mode` | string | `"empty_recall"` | Fallback on failure: `empty_recall`, `error`, `cache` |
| `circuit_breaker.failure_threshold` | int | `5` | Failures before circuit opens |
| `circuit_breaker.recovery_timeout_seconds` | float | `30.0` | Seconds before half-open attempt |
| `circuit_breaker.half_open_max_calls` | int | `2` | Max calls in half-open state |

---

## recall_cache

Similarity-based caching for repeated queries.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `false` | Enable recall cache |
| `similarity_threshold` | float | `0.95` | Cache hit threshold |
| `max_entries` | int | `256` | Max cached results |
| `ttl_seconds` | float | `300.0` | Cache entry lifetime (seconds) |

---

## tiered_retrieval

Progressive retrieval strategy — tries cheaper/faster tiers first.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `false` | Enable tiered retrieval |
| `min_results` | int | `3` | Min results per tier before advancing |
| `min_score` | float | `0.3` | Min relevance score threshold |
| `max_tier` | int | `3` | Maximum tier (0–4) |
| `full_recall` | `"pipeline"` \| `"hybrid"` | `"pipeline"` | Recall path for tier 3+. `hybrid` requires a hybrid engine provider |

---

## recall_authority

Structured truth precedence — labels fused hits for synthesis.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `false` | Enable recall authority |
| `rules_inline` | string \| null | `null` | Inline authority rules |
| `rules_path` | string \| null | `null` | Path to authority rules file |
| `tiers` | list | `[]` | Precedence tiers: `[{id: "primary", priority: 1, label: "Verified"}]` |
| `tier_by_bank` | dict | `{}` | Map bank IDs to tier IDs: `{bank-1: "primary"}` |
| `apply_to_reflect` | bool | `true` | Inject authority context into reflect prompts |

```yaml
recall_authority:
  enabled: true
  tiers:
    - id: primary
      priority: 1
      label: "Verified sources"
    - id: secondary
      priority: 2
      label: "Inferred knowledge"
  tier_by_bank:
    verified-bank: primary
    inferred-bank: secondary
  apply_to_reflect: true
```

---

## curated_retain

LLM-scored selective retention — only stores content above an importance threshold.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `false` | Enable curated retain |
| `model` | string \| null | `null` | LLM model for importance scoring |
| `context_recall_limit` | int | `5` | Max context items for scoring |

## curated_recall

Multi-factor ranking — blends recency, reliability, salience, and similarity.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `false` | Enable curated recall |
| `freshness_weight` | float | `0.3` | Recency bonus weight |
| `reliability_weight` | float | `0.2` | Authority/source weight |
| `salience_weight` | float | `0.2` | Relevance/importance weight |
| `original_score_weight` | float | `0.3` | Vector similarity weight |
| `freshness_half_life_days` | float | `30.0` | Decay curve for recency |
| `min_score` | float \| null | `null` | Min final score threshold |

---

## access_control

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `false` | Enable ACL enforcement |
| `default_policy` | string | `"owner_only"` | Default when no grants match: `owner_only`, `open`, `deny` |

## identity

Identity-driven bank resolution.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `auto_resolve_banks` | bool | `false` | Auto-create banks from principal |
| `user_bank_prefix` | string | `"user-"` | Prefix for user-scoped banks |
| `agent_bank_prefix` | string | `"agent-"` | Prefix for agent-scoped banks |
| `service_bank_prefix` | string | `"service-"` | Prefix for service-scoped banks |
| `resolver` | `"convention"` \| `"config"` \| `"custom"` \| null | `null` | Bank resolution strategy |
| `obo_enabled` | bool | `false` | Enable on-behalf-of permission intersection |

## access_grants

Top-level access grants (merged with per-bank `banks.*.access`).

```yaml
access_grants:
  - bank_id: "shared-*"
    principal: "agent:support-bot"
    permissions: [read, write]
  - bank_id: "user-alice"
    principal: "user:alice"
    permissions: [read, write, forget, admin]
```

| Field | Type | Description |
|-------|------|-------------|
| `bank_id` | string | Bank ID or glob pattern (`*` for all) |
| `principal` | string | Principal: `user:X`, `agent:X`, `service:X`, or `*` |
| `permissions` | list\[string\] | Permissions: `read`, `write`, `forget`, `admin`, `*` |

---

## dlp

Data Loss Prevention — output scanning for PII in recall and reflect results.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `scan_recall_output` | bool | `false` | Scan recall results for PII |
| `scan_reflect_output` | bool | `false` | Scan reflect output for PII |
| `output_pii_action` | string | `"warn"` | Action on detected PII: `redact`, `reject`, `warn` |

---

## lifecycle

Automatic memory archival and deletion based on age and activity.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `false` | Enable lifecycle management |
| `ttl.archive_after_days` | int | `90` | Archive if not recalled in N days |
| `ttl.delete_after_days` | int | `365` | Delete if older than N days |
| `ttl.exempt_tags` | list\[string\] \| null | `null` | Tags that skip TTL (e.g. `pinned`, `compliance`) |
| `ttl.fact_type_overrides` | dict \| null | `null` | Override `archive_after_days` by fact type: `{world: 180, experience: null}` |

```yaml
lifecycle:
  enabled: true
  ttl:
    archive_after_days: 90
    delete_after_days: 365
    exempt_tags: [pinned, compliance]
    fact_type_overrides:
      world: 180
      experience: null          # never auto-archive
```

---

## defaults

Per-profile reasoning defaults — affect reflect synthesis behavior.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `skepticism` | int | `3` | How critical to source (1–5) |
| `literalism` | int | `3` | How literal vs. interpretive (1–5) |
| `empathy` | int | `3` | How empathetic in synthesis (1–5) |
| `preferred_fact_types` | list\[string\] \| null | `null` | Preference order: `[experience, world, observation]` |
| `tags` | list\[string\] \| null | `null` | Default tags for all retained content |

---

## observability

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `otel_enabled` | bool | `false` | Enable OpenTelemetry spans |
| `prometheus_enabled` | bool | `false` | Enable Prometheus metrics |
| `log_level` | string | `"info"` | Log level: `debug`, `info`, `warn`, `error` |

---

## mcp

MCP (Model Context Protocol) server settings — used by `astrocyte.mcp` and the `astrocyte-mcp` CLI.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `default_bank_id` | string \| null | `null` | Default bank for MCP calls |
| `expose_reflect` | bool | `true` | Allow reflect via MCP |
| `expose_forget` | bool | `false` | Allow forget via MCP |
| `expose_admin` | bool | `false` | Allow lifecycle, bank health, and legal-hold admin tools via MCP |
| `max_results_limit` | int | `50` | Max items returned per request |
| `principal` | string \| null | `null` | Principal for MCP operations |

---

## mip_config_path

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `mip_config_path` | string \| null | `null` | Path to MIP routing rules file (`./mip.yaml`) |

See [Memory Intent Protocol](/design/memory-intent-protocol/) for the full MIP DSL — match operators, actions, override hierarchy, and intent policy.

---

## banks

Per-bank overrides. Each key is a bank ID. Any top-level section can be overridden per bank.

```yaml
banks:
  sensitive-bank:
    profile: research
    barriers:
      pii:
        mode: rules_then_llm
        action: reject
    homeostasis:
      recall_max_tokens: 2048
      rate_limits:
        recall_per_minute: 60
    signal_quality:
      dedup:
        similarity_threshold: 0.90
    access:
      - bank_id: sensitive-bank
        principal: "agent:analyst"
        permissions: [read, write]
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `profile` | string \| null | `null` | Override profile for this bank |
| `access` | list\[dict\] \| null | `null` | Bank-specific access grants |
| `homeostasis` | HomeostasisConfig \| null | `null` | Override homeostasis settings |
| `barriers` | BarrierConfig \| null | `null` | Override barrier settings |
| `signal_quality` | SignalQualityConfig \| null | `null` | Override signal quality settings |

---

## sources

External data source definitions for ingestion. See [poll ingest guide](poll-ingest-gateway/) for webhook, stream, and poll setup.

```yaml
sources:
  my-webhook:
    type: webhook
    extraction_profile: builtin_text
    target_bank: webhook-data
    auth:
      type: hmac_sha256
      secret: ${WEBHOOK_SECRET}

  github-issues:
    type: poll
    driver: github
    path: owner/repo
    interval_seconds: 300
    target_bank: github-issues
    auth:
      token: ${GITHUB_PAT}
      type: bearer

  redis-events:
    type: stream
    driver: redis
    url: redis://localhost:6379
    topic: my-stream
    consumer_group: astrocyte-group
    target_bank: events

  external-api:
    type: proxy
    url: "https://api.example.com/search?q={query}"
    target_bank: external
```

| Key | Type | Description |
|-----|------|-------------|
| `type` | string | Source type: `webhook`, `stream`, `poll` / `api_poll`, `proxy` |
| `driver` | string \| null | Driver name: `github`, `redis`, `kafka` |
| `extraction_profile` | string \| null | Extraction profile name for ingested content |
| `target_bank` | string \| null | Destination bank ID |
| `target_bank_template` | string \| null | Template: `"bank-{source_id}"` |
| `auth` | dict \| null | Auth config (type-specific: `hmac_sha256`, `bearer`, etc.) |
| `path` | string \| null | Source-specific path (e.g. `owner/repo` for GitHub) |
| `url` | string \| null | Source URL (Redis URL, Kafka bootstrap servers, proxy endpoint) |
| `topic` | string \| null | Stream topic or Redis stream key |
| `consumer_group` | string \| null | Consumer group name |
| `interval_seconds` | int \| null | Poll interval (min 60 for GitHub) |
| `recall_method` | string \| null | Proxy only: `GET` (default) or `POST` |
| `recall_body` | dict \| null | Proxy POST only: request body template |

---

## agents

Registered agents with bank access and rate hints.

```yaml
agents:
  support-bot:
    principal: "agent:support-bot"
    default_bank: shared-support
    banks: [shared-support, team-*]
    permissions: [read, write]
    max_retain_per_minute: 60
    max_recall_per_minute: 120
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `principal` | string \| null | `null` | Agent principal (e.g. `agent:my-bot`) |
| `banks` | list\[string\] \| null | `null` | Allowed bank IDs (glob patterns supported) |
| `allowed_banks` | list\[string\] \| null | `null` | Alias for `banks` |
| `default_bank` | string \| null | `null` | Default bank when not specified |
| `permissions` | list\[string\] \| null | `null` | Declared permissions (documentation/validation) |
| `max_retain_per_minute` | int \| null | `null` | Per-agent retain rate hint |
| `max_recall_per_minute` | int \| null | `null` | Per-agent recall rate hint |

---

## extraction_profiles

Reusable extraction configurations for ingestion sources.

```yaml
extraction_profiles:
  conversation:
    chunking_strategy: dialogue
    entity_extraction: llm
    content_type: text/plain
    chunk_size: 512
    fact_type: experience
    metadata_mapping:
      speaker: "$.participant_name"
    tag_rules:
      - match: { source: slack }
        tags: [slack, chat]
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `content_type` | string \| null | `null` | Expected content type |
| `chunking_strategy` | string \| null | `null` | Strategy: `sentence`, `paragraph`, `fixed`, `dialogue` |
| `chunk_size` | int \| null | `null` | Max characters per chunk |
| `entity_extraction` | bool \| string \| null | `null` | Extract entities: `true`, `false`, `ner`, `llm` |
| `fact_type` | string \| null | `null` | Default fact type: `world`, `experience`, `observation` |
| `authority_tier` | string \| null | `null` | Recall authority tier ID (overrides `recall_authority.tier_by_bank`) |
| `metadata_mapping` | dict \| null | `null` | Map source fields to metadata keys |
| `tag_rules` | list\[dict\] \| null | `null` | Generate tags from metadata patterns |

Built-in profiles: `builtin_text` and `builtin_conversation`.

---

## deployment

Standalone gateway settings — ignored in library mode.

```yaml
deployment:
  mode: standalone
  host: "0.0.0.0"
  port: 8000
  workers: 4
  cors_origins:
    - "https://example.com"
    - "http://localhost:3000"
  tls:
    cert_path: /path/to/cert.pem
    key_path: /path/to/key.pem
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `mode` | `"library"` \| `"standalone"` \| `"plugin"` | `"library"` | Deployment mode |
| `host` | string \| null | `null` | Bind address (standalone only) |
| `port` | int \| null | `null` | Port (standalone only) |
| `workers` | int \| null | `null` | Worker processes (standalone only) |
| `cors_origins` | list\[string\] \| null | `null` | CORS allowed origins |
| `tls.cert_path` | string \| null | `null` | TLS certificate path |
| `tls.key_path` | string \| null | `null` | TLS private key path |

---

## Compliance profiles

Pre-built compliance presets that configure barriers, lifecycle, access control, and DLP. Set `compliance_profile` at the top level — your explicit config overrides any values the profile sets.

| Profile | PII mode | PII action | Lifecycle | Access default | DLP |
|---------|----------|------------|-----------|----------------|-----|
| `pdpa` | `rules_then_llm` | `redact` | 5-year retention | `owner_only` | Reflect output scanned |
| `gdpr` | `rules_then_llm` | `redact` | 2-year retention | `deny` | Reflect output scanned |
| `hipaa` | `rules_then_llm` | `reject` | 7-year retention | `deny` | Recall + reflect scanned |

---

## Environment variable substitution

Any string value in `astrocyte.yaml` can reference environment variables with `${VAR_NAME}`:

```yaml
vector_store_config:
  dsn: ${DATABASE_URL}
llm_provider_config:
  api_key: ${OPENAI_API_KEY}
sources:
  my-webhook:
    auth:
      secret: ${WEBHOOK_SECRET}
```

Unresolved variables (not set in the environment) are left as the literal string `${VAR_NAME}`.

---

## Minimal examples

### Development (in-memory, no LLM)

```yaml
provider_tier: storage
vector_store: in_memory
llm_provider: mock
barriers:
  pii:
    mode: disabled
```

### Production (pgvector + OpenAI + PDPA compliance)

```yaml
profile: personal
provider_tier: storage
vector_store: postgres
vector_store_config:
  dsn: ${DATABASE_URL}

llm_provider: openai
llm_provider_config:
  api_key: ${OPENAI_API_KEY}
  model: gpt-4o-mini

compliance_profile: pdpa
mip_config_path: ./mip.yaml

lifecycle:
  enabled: true
  ttl:
    archive_after_days: 90
    delete_after_days: 365

access_control:
  enabled: true
  default_policy: owner_only
```

---

## Further reading

- [Memory API reference](memory-api-reference/) — retain/recall/reflect/forget signatures and examples
- [Authentication setup](authentication-setup/) — auth modes, OIDC providers, JWT claim mapping
- [Storage backend setup](storage-backend-setup/) — pgvector, Qdrant, Neo4j, Elasticsearch install and config
- [Monitoring & observability](monitoring-and-observability/) — health endpoints, logging, tracing, metrics
- [Access control setup](access-control-setup/) — grants, OBO, common patterns
- [Bank management](bank-management/) — bank creation, multi-bank queries, lifecycle recipes
- [MIP developer guide](/plugins/mip-developer-guide/) — writing and testing MIP routing rules
- [Production-grade HTTP service](production-grade-http-service/) — full production checklist
