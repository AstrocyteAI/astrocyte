# How Astrocyte works

A mid-level overview of Astrocyte's architecture — what happens when you call retain, recall, or reflect, and how the pieces fit together.

---

## The five operations

Every interaction with Astrocyte uses one of five operations:

```
┌─────────────────────────────────────────────────┐
│                  Your agent / app                │
│  (LangGraph, CrewAI, MCP, REST client, …)       │
└──────────────┬──────────────────────────────────┘
               │  retain / recall / reflect / forget / audit
               ▼
┌─────────────────────────────────────────────────┐
│                   Astrocyte                      │
│                                                  │
│  ┌──────────┐  ┌──────────┐  ┌───────────────┐  │
│  │  Policy   │  │ Pipeline │  │   Provider    │  │
│  │  layer    │→ │  stages  │→ │   adapters    │  │
│  └──────────┘  └──────────┘  └───────────────┘  │
└──────────────────────────────┬──────────────────┘
                               │
               ┌───────────────┼───────────────┐
               ▼               ▼               ▼
          pgvector          Apache AGE       Qdrant
         (vector)            (graph)        (vector)
```

| Operation | What it does |
|-----------|-------------|
| **retain** | Ingest text → extract entities and facts → resolve entities → deduplicate → chunk → embed → store in a memory bank |
| **recall** | Parse query → search vector + keyword + graph stores → rerank → return scored hits; optional `as_of` for point-in-time queries |
| **reflect** | Run recall, then pass hits to an LLM to synthesize a natural-language answer |
| **forget** | Soft-delete memories — writes `forgotten_at` timestamp; hard delete available; compliance audit support |
| **audit** | Reason about absence — scan coverage of a scope in a bank, surface missing topics and thin areas, return `AuditResult(gaps, coverage_score)` |

The **audit** operation is what separates Astrocyte from a retrieval index. A retrieval system finds what exists; audit surfaces what doesn't.

---

## Memory banks

A **bank** is an isolated namespace for memories. Think of it as a folder — each bank has its own access control, rate limits, and PII policies.

**Common patterns:**

- **One bank per user** — tenant isolation in a SaaS app (`user-alice`, `user-bob`)
- **Shared team bank** — collective knowledge for a team (`team-engineering`)
- **Agent-scoped bank** — each agent gets its own memory (`agent-researcher`, `agent-reviewer`)
- **Layered** — personal bank takes priority, falls back to team, then global

Banks are created automatically on first `retain()`, or explicitly in `astrocyte.yaml`. See [Bank management](bank-management/) for multi-bank queries and lifecycle patterns.

---

## The retain pipeline

When you call `retain()`, content flows through several stages:

1. **Policy check** — access control, rate limits, token budgets
2. **PII scanning** — regex and optional LLM-based detection; redact, warn, or reject
3. **Fact extraction** — break content into discrete facts (configurable profiles)
4. **Entity extraction + resolution** — extract named entities; query the graph store for candidates; LLM confirms matches with evidence quote; write `EntityLink(type="alias_of", evidence=...)` to graph
5. **Deduplication** — skip facts that already exist in the bank
6. **Chunking** — split into embeddable pieces (sentence, dialogue, or fixed-size)
7. **Embedding** — convert chunks to vectors via the configured LLM provider
8. **Storage** — write to the configured backend; `retained_at` system timestamp is set at this point

Each stage is configurable. The default pipeline works out of the box — override only what you need.

The entity resolution stage (step 4) is what enables cross-document identity unification. Without it, "Calvin" and "the CTO" remain separate memories with no link. With it, they become connected nodes in the graph, retrievable together and auditable as the same entity.

---

## The recall pipeline

When you call `recall()`:

1. **Query analysis** — extract intent, entities, and keywords
2. **Time-travel filter** — if `as_of` is set, apply `retained_at <= as_of` and `forgotten_at > as_of` (or `forgotten_at IS NULL`) to scope the search to the bank's historical state
3. **Multi-store search** — semantic (vector similarity), keyword (BM25), and optional graph neighborhood
4. **Fusion** — merge results from all stores using reciprocal rank fusion (RRF)
5. **Reranking** — boost proper nouns, keyword overlap, and recency
6. **Policy filtering** — enforce access control and DLP on returned hits
7. **Token budgeting** — trim results to stay within the caller's token budget

For multi-bank queries, you choose a **strategy**: `parallel` (search all banks at once), `cascade` (search in order, stop when enough results), or `first_match` (return from the first bank with hits).

**Time travel example:**
```python
# What did the team know about the deployment strategy before the incident?
hits = await brain.recall(
    "deployment strategy",
    bank_id="eng-team",
    as_of=datetime(2026, 3, 1, tzinfo=timezone.utc),
)
```

---

## The audit operation

`brain.audit(scope, bank_id)` asks: *given what's retained in this bank, what's missing about this topic?*

Unlike recall — which retrieves what exists — audit reasons about absence. It:

1. **Runs a scoped recall** — fetch what the bank knows about the scope topic
2. **Invokes an LLM judge** — given these memories, what should be here that isn't? What's thin? What contradicts?
3. **Returns** `AuditResult(gaps: list[GapItem], coverage_score: float)`

```python
result = await brain.audit("incident response procedures", bank_id="eng-team")
# AuditResult(
#   gaps=[
#     GapItem(topic="rollback procedures", severity="high", reason="no memories matching rollback or revert"),
#     GapItem(topic="escalation path for database incidents", severity="medium", reason="only one memory, from 2025"),
#   ],
#   coverage_score=0.54,
# )
```

The `coverage_score` is a 0–1 float. Below 0.7 indicates significant gaps. Operators can alert on low scores for critical knowledge areas (compliance procedures, runbooks, customer commitments).

---

## The policy layer

Every operation passes through the policy layer before touching storage. The policy layer enforces:

| Policy | What it does |
|--------|-------------|
| **Access control** | Principals, permissions, and per-bank grants |
| **PII barriers** | Detect and handle personally identifiable information |
| **Rate limits** | Per-bank and per-principal throttling |
| **Token budgets** | Cap LLM token usage per operation |
| **Deduplication** | Prevent storing redundant content |
| **Signal quality** | Minimum relevance thresholds for recall |

Policies are configured in `astrocyte.yaml` at the instance level and can be overridden per bank. See [Configuration reference](configuration-reference/) for the full schema.

---

## Providers (pluggable backends)

Astrocyte uses a **two-tier provider model**:

**Tier 1 — Storage providers** (you pick your backend):
- **VectorStore** — semantic search (pgvector, Qdrant, Elasticsearch)
- **GraphStore** — relationship queries (Neo4j)
- **DocumentStore** — full-text / keyword search (Elasticsearch, BM25)

Astrocyte runs its own pipeline (chunking, embedding, reranking, synthesis) and calls providers for storage and retrieval.

**Tier 2 — Memory engine providers** (the engine owns the pipeline):
- Full memory engines like Mem0 or custom implementations
- Astrocyte delegates retain/recall to the engine and applies policy around it

Most users start with Tier 1. See [Storage backend setup](storage-backend-setup/) for configuring backends.

---

## MIP: declarative routing

The **Memory Intent Protocol (MIP)** lets you declare rules that automatically route memories to the right bank with the right tags and policies — without routing logic in your application code.

```yaml
rules:
  - name: support-tickets
    match:
      content_type: support_ticket
    action:
      bank: "support-{metadata.customer_id}"
      tags: [support]
```

Mechanical rules resolve deterministically with zero LLM cost. When no rule matches, an optional intent layer can ask an LLM to decide. See [MIP developer guide](/plugins/mip-developer-guide/) for the full DSL.

---

## Deployment models

| Model | When to use |
|-------|-------------|
| **Library** | Embed in your Python app — `brain = Astrocyte.from_config("astrocyte.yaml")` |
| **Standalone gateway** | REST API via Docker — agents call `/v1/retain`, `/v1/recall`, `/v1/reflect` |
| **Gateway plugin** | Add memory to existing Kong/APISIX/Azure APIM — intercepts `/chat/completions` |

All three models use the same core, policy layer, and providers. The gateway adds HTTP auth, rate limiting, and health endpoints. See [Quick Start](quick-start/) for setup instructions.

---

## Further reading

- [Quick Start](quick-start/) — install and run your first retain/recall
- [Memory API reference](memory-api-reference/) — full signatures and examples
- [Configuration reference](configuration-reference/) — complete `astrocyte.yaml` schema
- [Bank management](bank-management/) — multi-bank queries and lifecycle
- [Architecture](/design/architecture/) — full technical architecture (design docs)
