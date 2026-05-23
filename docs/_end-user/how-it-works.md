# How Astrocyte works

A mid-level overview of Astrocyte's architecture — what happens when you call retain, recall, or reflect, and how the pieces fit together.

---

## The core operations

Most interactions with Astrocyte use the memory operations below. `retain`, `recall`, `reflect`, and `forget` are the basic loop; `history`, `audit`, `compile`, and the M21 live-memory surface (`create_directive`, mental model CRUD, observation CRUD) extend it.

```
┌─────────────────────────────────────────────────┐
│                  Your agent / app                │
│  (LangGraph, CrewAI, MCP, REST client, …)       │
└──────────────┬──────────────────────────────────┘
               │  retain / recall / reflect / forget / history / audit / compile
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
          pgvector          Neo4j          Qdrant
         (vector)     (graph, optional)   (vector)
```

| Operation | What it does |
|-----------|-------------|
| **retain** | Ingest text → route through Document or Conversation Engine → extract entities and facts → deduplicate → embed → store in a memory bank |
| **recall** | Parse query → search vector + keyword + optional graph stores → RRF fusion → rerank → return scored hits; optional `as_of` for point-in-time queries |
| **reflect** | Runs an agentic recall loop and synthesizes a finished natural-language answer with citations |
| **forget** | Soft-delete memories — writes `forgotten_at` timestamp; hard delete available; compliance audit support |
| **history** | Convenience wrapper around `recall(as_of=...)` for point-in-time snapshots |
| **audit** | Reason about absence — scan coverage of a scope in a bank, surface missing topics and thin areas, return `AuditResult(gaps, coverage_score)` |
| **compile** | Optional wiki compile that materializes topic pages from raw memories for higher-quality recall |
| **create_directive** | Author a user-curated hard rule stored as a `MentalModel(kind="directive")` — consulted at recall time before raw memories |
| **create/update/delete mental model** | CRUD for curated, refreshable summaries with structured-doc schema and typed delta operations (M21) |
| **create/list/delete observation** | CRUD for autonomously or manually authored observations with trend tracking (M21) |

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

## The three engines (M17+)

Astrocyte routes incoming content through one of two dedicated ingest engines before the Memory Engine stores it. Choosing the right engine gives the Memory Engine richer structural signals at recall time.

| Engine | Input shape | What it preserves |
|--------|-------------|-------------------|
| **Document Engine** | PDFs, markdown, long-form text | PageIndex tree (title + nested section hierarchy, line anchors, adaptive summaries) |
| **Conversation Engine** | Chat transcripts, turn arrays | Speaker identity, turn ordering, session boundaries, per-turn timestamps |
| **Memory Engine** | Output of either engine | Facts, observations, mental models, section links, wiki pages, embeddings |

Select the engine via `content_type` on `retain()`: `"text"` / `"document"` routes through the Document Engine; `"conversation"` routes through the Conversation Engine. Both write to the same Memory Engine downstream — so a chat session with an attached PDF can be ingested through both engines into one bank.

---

## The retain pipeline

When you call `retain()`, content flows through several stages:

1. **Policy check** — access control, rate limits, token budgets
2. **PII scanning** — regex and optional LLM-based detection; redact, warn, or reject
3. **Engine routing** — `content_type` selects Document Engine (tree extraction) or Conversation Engine (turn-aware chunking)
4. **Fact + entity extraction** — break content into discrete facts; optionally extract named entities and resolve aliases via LLM with evidence quotes
5. **Deduplication** — skip facts that already exist in the bank
6. **Embedding** — convert chunks to vectors via the configured LLM provider
7. **Storage** — write to the configured backend; `retained_at` system timestamp is set at this point
8. **Observation consolidation** — autonomous consolidator runs in the background; observations accumulate evidence and acquire computed trends (NEW / STRENGTHENING / STABLE / WEAKENING / STALE)

Each stage is configurable. The default pipeline works out of the box — override only what you need.

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

Astrocyte supports **two kinds of providers**:

**Storage providers** (you pick your backend; Astrocyte's pipeline owns recall):
- **VectorStore** — semantic search (pgvector, Qdrant)
- **GraphStore** — relationship-aware recall and entity resolution (Neo4j). Optional — graph traversal over flat SQL tables (section_links, section_entities) is built-in without any graph adapter.
- **DocumentStore** — full-text / keyword search (Elasticsearch, BM25)

Astrocyte runs its own pipeline (chunking, embedding, reranking, synthesis) and calls providers for storage and retrieval.

**Engine providers** (the engine owns the pipeline):
- Full memory engines like Mem0 or custom implementations
- Astrocyte delegates retain/recall to the engine and applies policy around it

Most users start with storage providers. See [Storage backend setup](storage-backend-setup/) for configuring backends.

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
