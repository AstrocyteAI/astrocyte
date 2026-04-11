# Memory Intent Protocol (MIP)

A declarative protocol that governs memory routing decisions across an agent system. "Intent" carries two senses: the **system's declared intent** — routing rules, compliance policies, and escalation paths expressed as configuration — and the **model's expressed intent** — the LLM's judgment about what to remember, where, and why, invoked when deterministic rules cannot resolve.

MIP resolves mechanically where possible, delegates to model judgment where not.

Extends the [MCP server](mcp-server.md). Interacts with the [policy layer](policy-layer.md). Enforces [access control](access-control.md).

---

## 1. Why MIP exists

MCP (Level 1) gives agents memory tools. The agent calls `memory_retain` and `memory_recall` as MCP tools. For simple single-agent cases, this is sufficient — the LLM reads tool descriptions and routes correctly.

MCP alone breaks down when:

1. **Multiple agents share memory** and need consistent routing — each agent independently decides which bank, which tags, which compliance profile. Routing logic duplicates and diverges.
2. **Compliance requires enforcement, not suggestion** — MCP tool descriptions are prompts. PDPA, GDPR, and HIPAA need enforceable constraints, not model-dependent behavior.
3. **Routing logic drifts across system prompts** — the same routing policy appears in multiple agent system prompts and becomes untestable, unauditable, load-bearing prose.

MIP is what you add on top of MCP when the "declare the tool, trust the model" assumption stops holding.

---

## 2. Three levels of MCP-principle application

| Level | Principle applied | Astrocyte surface | Status |
|---|---|---|---|
| 1 | Tool contracts — LLM decides when to call | `astrocyte-mcp` over stdio/SSE | Implemented |
| 2 | Provider contracts — framework handles pipeline | Tier 1 / Tier 2 SPI + entry points | Implemented |
| 3 | Declarative routing — rules + model judgment | MIP (`mip.yaml`) | This document |

---

## 3. MIP schema

MIP is declared in `mip.yaml`, separate from `astrocyte.yaml` (which is infrastructure config). They have different owners and change cadences:

| File | Owned by | Changes when |
|---|---|---|
| `astrocyte.yaml` | Platform / infra team | Backend swapped, new provider, compliance profile changes |
| `mip.yaml` | Agent / domain team | New content types, new routing rules, domain model evolves |

**Not RAG authority tiers:** MIP governs **where retains go** and **mechanical routing** — not *precedence among graph vs vector vs text stores at recall time* (truth / authority layers). For terminology separating **cost escalation** (`tiered_retrieval` in `astrocyte.yaml`) from **structured precedence** recall, see `built-in-pipeline.md` §9.4 and **`product-roadmap-v1.md` § M7** (**v0.8.0**, with **M5**).

### 3.1 Bank definitions

What memory banks exist, who can access them, and which compliance profile applies.

```yaml
version: 1.0

banks:
  - id: "student-{student_id}"
    description: Per-student academic memory
    access: [agent:tutor, agent:grader]
    compliance: pdpa

  - id: ops-monitoring
    description: Pipeline operational memory
    access: [agent:monitor, agent:incident]
    compliance: pdpa
```

### 3.2 Mechanical rules

Priority-ordered, deterministic routing rules. Evaluated in order — first match wins (unless `override: true`, which always wins regardless of order).

```yaml
rules:
  # Hard compliance rules — MIP intent layer cannot override these
  - name: pii-lockdown
    priority: 1
    override: true
    match:
      any:
        - pii_detected: true
        - metadata.classification: sensitive
    action:
      bank: private-encrypted
      tags: [pdpa, pii]
      retain_policy: redact_before_store

  # Content-type driven routing
  - name: student-answer
    priority: 10
    match:
      all:
        - content_type: student_answer
        - metadata.student_id: present
    action:
      bank: "student-{metadata.student_id}"
      tags:
        - "{metadata.topic}"
        - "{metadata.difficulty}"
        - "attempt-{metadata.attempt_number}"

  # Source-driven routing
  - name: fabric-pipeline-event
    priority: 10
    match:
      all:
        - source: fabric_pipeline
        - metadata.pipeline_id: present
    action:
      bank: ops-monitoring
      tags: [fabric, "{metadata.pipeline_id}", "{metadata.status}"]

  # Fallback — no rule matched, escalate to intent layer
  - name: unmatched-fallback
    priority: 999
    match:
      all: []               # always matches
    action:
      escalate: mip
```

### 3.3 Match DSL

The match block supports existence checks, value checks, and boolean composition.

**Existence checks:**
```yaml
match:
  metadata.student_id: present
  metadata.pipeline_id: absent
```

**Value checks:**
```yaml
match:
  content_type: student_answer
  metadata.status:
    in: [failed, error, timeout]
```

**Boolean composition:**
```yaml
match:
  all:
    - content_type: student_answer
    - metadata.student_id: present
  any:
    - metadata.difficulty: hard
    - metadata.attempt_number:
        gte: 3
  none:
    - pii_detected: true
```

**Computed signals** (cheap, non-LLM):
```yaml
match:
  signals.word_count:
    gte: 50
  signals.novelty_score:
    gte: 0.7
```

Novelty score and word count are computed by the Astrocyte pipeline, not by LLM inference. The moment a signal requires semantic understanding, it belongs in the intent layer.

### 3.4 Action DSL

```yaml
action:
  bank: "student-{metadata.student_id}"     # template interpolation
  tags:
    - "{metadata.topic}"
    - static-tag
  retain_policy: default                     # or: redact_before_store, encrypt, reject
  escalate: none                             # or: mip (hand off to intent layer)
  confidence: 0.95                           # signals to ambiguity detector
```

### 3.5 Intent policy

What the model sees when mechanical rules cannot resolve. Only fires on escalation.

```yaml
intent_policy:
  escalate_when:
    - matched_rules: 0
    - confidence: lt 0.8
    - conflicting_rules: true

  model_context: |
    You are a memory routing agent. Route content to the correct
    bank and apply appropriate tags. Available banks: {banks}.
    Tag taxonomy: {tags}. Never override compliance rules.

  constraints:
    - cannot_override: [pii-lockdown]
    - must_justify: true
    - max_tokens: 200
```

### 3.6 Retrieval and reflect triggers

Conditions under which MIP triggers auto-recall or auto-reflect.

```yaml
retrieval_policy:
  auto_recall_when:
    - query_references_past: true
    - agent: tutor
      context_tokens_remaining: lt 2000

  auto_reflect_when:
    - same_topic_recall_count: gte 5
    - agent: monitor
      anomaly_pattern_detected: true
```

---

## 4. Resolution pipeline

One resolution pipeline with two strategies:

```
Content + metadata + context
          │
          ▼
    MIP Rule Engine
    (mechanical section)
          │
          ├── override: true match → execute, halt (MIP intent locked out)
          │
          ├── confident match → execute, halt
          │
          └── escalation condition met
                    │
                    ▼
            MIP Intent Layer
            (model judgment,
             constrained by
             intent_policy)
                    │
                    ▼
              Routing decision
                    │
                    ▼
               Astrocyte
```

From Astrocyte's perspective, it receives a `RoutingDecision` object regardless of which path resolved it. MIP is the single boundary.

---

## 5. Override hierarchy

Explicit rules for who can override whom:

| Rule type | Intent layer can override? | Rationale |
|---|---|---|
| Compliance-mandatory (`override: true`) | **Never** | PDPA, GDPR — non-negotiable |
| High-confidence mechanical match | **No** | Unnecessary LLM cost |
| Low-confidence mechanical match | **Yes** | Intent layer's reason to exist |
| No rule matched | **Yes** | Intent layer's primary use case |
| Intent layer decision | Mechanical cannot override | Intent fires after mechanical |

Compliance rules are always mechanical, never delegated to model judgment.

---

## 6. Relationship to existing Astrocyte components

| Component | Relationship to MIP |
|---|---|
| `astrocyte.yaml` | Infrastructure config — MIP reads bank definitions and provider settings from here |
| `policy-layer.md` | PII scanning, rate limits, token budgets apply *after* MIP routes the operation |
| `access-control.md` | MIP bank definitions compose with access control grants |
| `mcp-server.md` | MIP enhances the MCP server with routing enforcement |
| `built-in-pipeline.md` | The retain/recall pipeline executes after MIP resolves routing |
| `curated retain` | LLM-curated retain (innovations §2.2) is a subset of MIP's intent layer |
| `tiered retrieval` | Progressive escalation (innovations §2.1) is orthogonal to MIP routing |

---

## 7. When to use MIP vs MCP alone

| Scenario | Use |
|---|---|
| Single agent, simple bank structure, low compliance pressure | MCP alone |
| Multiple agents sharing memory, routing inconsistencies emerging | MIP |
| Compliance requires enforcement, not suggestion | MIP |
| Routing logic duplicated across system prompts | MIP |
| High-frequency structured events (pipeline monitoring) | MIP mechanical rules |
| Ambiguous content needing semantic classification | MIP intent layer |

The trigger for MIP: when routing logic appears in more than one system prompt, or when a compliance requirement needs enforcement not suggestion.
