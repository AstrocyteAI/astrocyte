# Use-case profiles: specialized astrocyte subtypes

This document defines how the heterogeneity principle (Principle 4 from `design-principles.md`) is realized as **configuration profiles** - pre-built policy configurations tuned for specific agent use cases.

---

## 1. The heterogeneity principle

**Biology:** Astrocytes differ by region and lineage. Fibrous astrocyte in white matter behave differently from protoplasmic astrocyte in gray matter. Reactive phenotypes (A1/A2) are context-dependent, not one-size-fits-all.

**Engineering:** Different agent types need different memory governance. A customer support agent storing sensitive conversations has different needs than a coding assistant storing technical context. Rather than force every user to configure policies from scratch, Astrocyte ships **profiles** - named presets that configure the policy layer for common use cases.

---

## 2. Profile mechanism

A profile is a named configuration overlay that sets defaults for the policy layer. Users can extend or override any setting.

```yaml
# astrocyte.yaml
profile: support                   # Load the "support" profile as baseline
provider: mystique

# Override specific settings from the profile
homeostasis:
  recall_max_tokens: 8192          # Override the profile's default of 4096
```

Profiles are shipped as YAML files inside the `astrocyte` package. They are not code - they are configuration presets.

Resolution order: `profile defaults` → `user config` → `per-bank overrides`. Later values win.

---

## 3. Shipped profiles

### 3.1 `support` - Customer support agents

Optimized for: handling user conversations with PII sensitivity, empathetic recall, experience-heavy memory.

```yaml
# profile: support
homeostasis:
  recall_max_tokens: 4096
  reflect_max_tokens: 4096
  retain_max_content_bytes: 51200
  rate_limits:
    retain_per_minute: 30
    recall_per_minute: 60
    reflect_per_minute: 10

barriers:
  pii:
    mode: regex
    action: redact
  validation:
    max_content_length: 10000
    reject_empty_content: true

signal_quality:
  dedup:
    enabled: true
    similarity_threshold: 0.92
  noisy_bank:
    enabled: true
    action: throttle

escalation:
  degraded_mode: empty_recall      # Agent can still function without memory

defaults:
  dispositions:
    skepticism: 2                  # Trust the customer's account
    literalism: 2                  # Flexible interpretation
    empathy: 5                     # High empathy
  preferred_fact_types:
    - experience                   # Prioritize what happened
    - world                        # Then general knowledge
  tags:
    - support
    - customer
```

**Why these choices:**
- PII redaction is non-negotiable in support contexts.
- High empathy disposition produces synthesis that acknowledges the user's experience.
- `empty_recall` degraded mode means the agent can still respond (just without personalization) when memory is down.
- Lower rate limits because support conversations are human-paced.

### 3.2 `coding` - Coding assistants

Optimized for: storing technical context, code patterns, error resolutions, world-fact-heavy memory.

```yaml
# profile: coding
homeostasis:
  recall_max_tokens: 8192          # Code context needs more tokens
  reflect_max_tokens: 8192
  retain_max_content_bytes: 204800 # 200KB - code files can be large
  rate_limits:
    retain_per_minute: 120         # High throughput for automated indexing
    recall_per_minute: 240
    reflect_per_minute: 30

barriers:
  pii:
    mode: regex
    action: warn                   # Warn but don't block - code has false positives
  validation:
    max_content_length: 100000
    reject_empty_content: true

signal_quality:
  dedup:
    enabled: true
    similarity_threshold: 0.98     # Higher threshold - similar code != duplicate
  noisy_bank:
    enabled: true
    action: warn

escalation:
  degraded_mode: error             # Code assistant should know memory is unavailable

defaults:
  dispositions:
    skepticism: 3                  # Neutral
    literalism: 5                  # High literalism - precision matters in code
    empathy: 1                     # Detached - technical context, not emotional
  preferred_fact_types:
    - world                        # Technical knowledge first
    - experience                   # Then project-specific learnings
  tags:
    - code
    - technical
```

**Why these choices:**
- PII in `warn` mode because code contains email-like strings, regex patterns, and test data that trigger false positives.
- Higher dedup threshold because similar code snippets are often meaningfully different.
- High literalism disposition because code synthesis must be precise.
- `error` degraded mode because a coding assistant giving wrong answers (without memory) is worse than saying "memory unavailable."

### 3.3 `personal` - Personal assistants

Optimized for: broad context, temporal awareness, balanced dispositions, conversational memory.

```yaml
# profile: personal
homeostasis:
  recall_max_tokens: 4096
  reflect_max_tokens: 8192
  retain_max_content_bytes: 51200
  rate_limits:
    retain_per_minute: 60
    recall_per_minute: 120
    reflect_per_minute: 30

barriers:
  pii:
    mode: regex
    action: redact
  validation:
    max_content_length: 20000
    reject_empty_content: true

signal_quality:
  dedup:
    enabled: true
    similarity_threshold: 0.93
  noisy_bank:
    enabled: true
    action: warn

escalation:
  degraded_mode: empty_recall

defaults:
  dispositions:
    skepticism: 3                  # Balanced
    literalism: 3                  # Balanced
    empathy: 4                     # Somewhat empathetic
  preferred_fact_types:
    - experience                   # User's personal experiences first
    - world
    - observation                  # Consolidated knowledge
  tags:
    - personal
```

### 3.4 `research` - Research and analysis agents

Optimized for: large document ingestion, high-volume recall, skeptical synthesis, graph-heavy retrieval.

```yaml
# profile: research
homeostasis:
  recall_max_tokens: 16384         # Research needs deep context
  reflect_max_tokens: 16384
  retain_max_content_bytes: 524288 # 512KB - research documents are large
  rate_limits:
    retain_per_minute: 200         # Batch document ingestion
    recall_per_minute: 300
    reflect_per_minute: 50

barriers:
  pii:
    mode: disabled                 # Research data is typically non-personal
  validation:
    max_content_length: 500000
    reject_empty_content: true

signal_quality:
  dedup:
    enabled: true
    similarity_threshold: 0.97
  scoring:
    enabled: true                  # Quality-gate research inputs
    min_score: 0.4
    action: tag
  noisy_bank:
    enabled: true
    action: warn

escalation:
  degraded_mode: error             # Research needs accurate recall, not empty

defaults:
  dispositions:
    skepticism: 5                  # Highly skeptical - verify claims
    literalism: 4                  # Precise interpretation
    empathy: 1                     # Detached - analytical
  preferred_fact_types:
    - world                        # Factual knowledge
    - observation                  # Synthesized insights
  tags:
    - research
    - analysis
```

### 3.5 `minimal` - Bare minimum, no policies

For users who want the provider abstraction but no governance. Useful for testing, development, or when the provider handles everything internally.

```yaml
# profile: minimal
homeostasis:
  recall_max_tokens: null          # No limit
  reflect_max_tokens: null
  retain_max_content_bytes: null
  rate_limits: null                # No rate limiting

barriers:
  pii:
    mode: disabled
  validation:
    reject_empty_content: true     # Only check: don't store nothing

signal_quality:
  dedup:
    enabled: false
  noisy_bank:
    enabled: false

escalation:
  degraded_mode: error

observability:
  otel_enabled: false
  prometheus_enabled: false
```

---

## 4. Custom profiles

Users can define their own profiles as YAML files and reference them by path:

```yaml
# astrocyte.yaml
profile: ./profiles/healthcare-agent.yaml
provider: mystique
```

Or inline, by omitting the `profile` key and configuring everything directly.

---

## 5. Per-bank profile overrides

A single Astrocyte instance can serve multiple banks with different profiles. This supports multi-tenant applications where each tenant (or agent) has different memory governance needs:

```yaml
# astrocyte.yaml
profile: support                   # Default for all banks
provider: mystique

banks:
  vip-customer:
    profile: support               # Explicit (same as default here)
    barriers:
      pii:
        mode: llm                  # Upgrade to LLM-based PII for VIP
        action: reject             # Stricter: reject, don't redact

  internal-testing:
    profile: minimal               # Different profile entirely

  research-team:
    profile: research
    homeostasis:
      recall_max_tokens: 32768     # Research team gets bigger budget
```

---

## 6. Profile selection guidance

| Agent type | Profile | Key tradeoff |
|---|---|---|
| Customer support, helpdesk | `support` | PII protection vs. recall fidelity |
| IDE assistant, code review | `coding` | Throughput vs. false-positive PII |
| Personal assistant, chat | `personal` | Breadth vs. privacy |
| Document analysis, RAG | `research` | Depth vs. cost (large token budgets) |
| Development, testing | `minimal` | Speed vs. safety |

Profiles are starting points. Production deployments should audit and tune settings based on observed metrics (see `policy-layer.md`, section 5: Observability).
