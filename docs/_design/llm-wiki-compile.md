# LLM wiki compile — compounding knowledge pages for Astrocyte

**Status:** Implemented in the `0.9.x` pre-GA feature line. See [product-roadmap.md (§ Release numbering)](product-roadmap.md) and the root changelog for release status.

**Origin:** Karpathy's [LLM wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) — "a structured, interlinked collection of markdown files that sits between you and the raw sources," with the LLM doing the bookkeeping (summaries, cross-references, lint) so humans only supply curation and direction.

This document adapts that pattern to Astrocyte's memory pipeline. It proposes a new memory class — **wiki pages** — maintained by an async **CompileEngine**, retrievable ahead of raw fragments, and evaluated head-to-head against the current recall path.

---

## 1. Why this exists

Astrocyte's current recall is effectively RAG over raw chunks: every query rediscovers the same fragments. `reflect()` synthesises at query time but the synthesis is thrown away. Nothing compounds.

Karpathy's observation: **the tedious part of a knowledge base is the bookkeeping**, not the reading. LLMs eliminate that friction. A persistent, LLM-maintained summary layer — entity pages, topic pages, concept pages — turns retrieval into *looking at a compiled page* instead of reconstructing one on every call.

This maps cleanly onto Astrocyte's **Principle 7 (pruning / consolidation)** — consolidation already exists as a lifecycle stage (`memory-lifecycle.md` §4). Wiki compile generalises it from *"merge facts into observations"* to *"maintain compounding topic pages with provenance, cross-links, and contradiction detection."*

---

## 2. Karpathy → Astrocyte mapping

| Karpathy's concept | Astrocyte equivalent | Status |
|---|---|---|
| Raw sources | Raw memories (`retain()` output) | Exists |
| Wiki pages (markdown) | **`WikiPage` — new memory class**, rewritable, with revision history | **New** |
| Schema (conventions file) | MIP config (`memory-intent-protocol.md`) | Exists |
| Ingest op | `retain()` **+ async compile side-effect** | Extends |
| Query op | `recall()` with **wiki-tier-first** precedence | Extends |
| Lint op | New periodic **lint pass** (contradictions, stale, orphans) | **New** |

---

## 3. Architecture: the CompileEngine

```
retain(raw memory)
    ↓
[threshold check]  ← per-bank: new memories since last compile + staleness
    │
    ├─ below threshold → done (no compile)
    │
    └─ at/above threshold → enqueue compile job (async, per-bank queue)
                                        ↓
                            CompileEngine.run(bank_id):
                                1. scope_discover(bank_id)  → list[CompileScope]
                                2. for each scope:
                                     fetch_memories(scope)
                                     synthesise(memories) → WikiPage revision
                                     write(page_revision)  ← rewritable; retains revision history
                                3. cross_link(pages_touched)  (entity → entity edges)
                                4. emit memory.compiled event
```

**Async, never blocks retain.** Retain latency must stay bounded; compile is eventual. Failures are retried and observable but do not fail the write path.

**Per-bank opt-in.** Compile is expensive (LLM cost × pages-touched × raw-memories). Turn it on per bank via MIP config; off by default.

---

## 3.1 Triggering model

Two triggers, same underlying machinery:

| Trigger | Who initiates | Scope input |
|---------|--------------|-------------|
| **Threshold** (automatic) | CompileEngine background worker | None — scope is *discovered* (§3.2) |
| **Explicit** `brain.compile(scope, bank_id)` | Caller | Scope string provided — discovery skipped |

The threshold fires when **both conditions are evaluated** after each retain:

- **Size:** ≥ N new memories accumulated since the last compile for this bank (default: 50)
- **Staleness:** last compile is older than T (default: 7 days) AND ≥ M new memories exist (default: 10)

Either condition alone is sufficient to enqueue. The thresholds are configurable per bank (§8).

---

## 3.2 Scope discovery (automatic trigger only)

When no caller-provided scope is available, the CompileEngine must discover what to compile. It operates in two passes over the uncompiled memories in the bank:

**Pass 1 — Tagged memories → group by tag**

Memories that carry tags (assigned at retain time via caller, extraction profile, or MIP routing) are grouped by tag. Each unique tag becomes a compile scope. This is cheap — no LLM, no clustering — and produces the most semantically coherent groups because tags were assigned at the point of knowledge.

**Pass 2 — Untagged memories → DBSCAN clustering**

Memories with no tags are clustered by embedding similarity using DBSCAN (density-based spatial clustering). DBSCAN is chosen over k-means because:
- Does not require knowing the number of clusters in advance
- Handles organic growth — new topics emerge without reconfiguration
- Naturally produces noise points (memories that don't cluster) rather than forcing them into wrong buckets

Each cluster is then labelled with a short topic string via a lightweight LLM call over the cluster's most representative memories. The label becomes the scope string on the resulting WikiPage.

```
untagged memories
    ↓
DBSCAN(vectors, eps=ε, min_samples=M)
    ↓
cluster_0, cluster_1, ..., cluster_n, noise
    ↓ (per cluster)
LLM("What is the topic of these memories?") → "incident response"
    ↓
compile scope = "incident response"
```

Noise points (memories DBSCAN could not assign to any cluster) are held for the next compile cycle, where they may cluster with newly retained memories.

**Unified scope list**: Pass 1 and Pass 2 results are merged into a single `list[CompileScope]`. The CompileEngine synthesises each scope identically regardless of whether it came from a tag or a cluster label.

---

## 4. The `WikiPage` memory class

```python
@dataclass
class WikiPage(Memory):
    """A compiled topic/entity/concept page synthesised from raw memories."""
    page_id: str                    # Stable id (e.g. "entity:alice", "topic:hiring-policy")
    kind: Literal["entity", "topic", "concept"]
    title: str
    content: str                    # LLM-maintained markdown
    source_ids: list[str]           # Raw memories that contributed (provenance)
    cross_links: list[str]          # Other page_ids referenced
    revision: int
    revised_at: datetime
    # Standard Memory fields (bank_id, tags, embeddings, …) inherited.
```

Key differences from raw memory:
- **Mutable in place** (revision-versioned, not append-only)
- **Has provenance** back to every contributing raw memory
- **Participates in recall** as a first-class memory kind — but with its own tier precedence

### 4.1 Storage

- **SQL wiki tables are the source of truth.** They store logical pages, immutable revisions, source provenance, wiki links, lint state, and compile metadata.
- **pgvector is the search projection.** The current page revision is indexed as a compiled memory (`memory_layer="compiled"`, `fact_type="wiki"`) so standard recall can retrieve it semantically.
- **Apache AGE is the traversal projection.** Page-to-page, page-to-entity, entity-to-memory, and page-to-memory edges are mirrored into AGE for bounded graph expansion.
- Past revisions remain in SQL for audit, lint, and debugging; they are not indexed for normal recall unless time-travel wiki recall is explicitly enabled.
- Forget semantics: forgetting a raw source triggers **recompile** of the pages that cited it — not tombstone-then-drift.

#### 4.1.1 Postgres schema sketch

```sql
CREATE TABLE astrocyte_wiki_pages (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  bank_id text NOT NULL,
  slug text NOT NULL,
  title text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('topic', 'entity', 'concept')),
  current_revision_id uuid,
  confidence double precision NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (bank_id, slug)
);

CREATE TABLE astrocyte_wiki_revisions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  page_id uuid NOT NULL REFERENCES astrocyte_wiki_pages(id) ON DELETE CASCADE,
  revision_number integer NOT NULL,
  markdown text NOT NULL,
  summary text,
  compiled_by text,
  source_count integer NOT NULL DEFAULT 0,
  tokens_used integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (page_id, revision_number)
);

ALTER TABLE astrocyte_wiki_pages
  ADD CONSTRAINT astrocyte_wiki_pages_current_revision_fk
  FOREIGN KEY (current_revision_id)
  REFERENCES astrocyte_wiki_revisions(id);

CREATE TABLE astrocyte_wiki_revision_sources (
  revision_id uuid NOT NULL REFERENCES astrocyte_wiki_revisions(id) ON DELETE CASCADE,
  memory_id text NOT NULL,
  bank_id text NOT NULL,
  quote text,
  relevance double precision,
  PRIMARY KEY (revision_id, memory_id)
);

CREATE TABLE astrocyte_wiki_links (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  from_page_id uuid NOT NULL REFERENCES astrocyte_wiki_pages(id) ON DELETE CASCADE,
  to_page_id uuid REFERENCES astrocyte_wiki_pages(id) ON DELETE SET NULL,
  target_slug text NOT NULL,
  link_type text NOT NULL DEFAULT 'related',
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (from_page_id, target_slug, link_type)
);
```

Lint state is stored separately so compile can stay focused on page materialization:

```sql
CREATE TABLE astrocyte_wiki_lint_issues (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  page_id uuid NOT NULL REFERENCES astrocyte_wiki_pages(id) ON DELETE CASCADE,
  revision_id uuid REFERENCES astrocyte_wiki_revisions(id) ON DELETE SET NULL,
  issue_type text NOT NULL,
  severity text NOT NULL CHECK (severity IN ('low', 'medium', 'high')),
  message text NOT NULL,
  evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL DEFAULT 'open',
  created_at timestamptz NOT NULL DEFAULT now(),
  resolved_at timestamptz
);
```

Recommended indexes:

```sql
CREATE INDEX astrocyte_wiki_pages_bank_kind_idx
  ON astrocyte_wiki_pages (bank_id, kind);

CREATE INDEX astrocyte_wiki_revisions_page_created_idx
  ON astrocyte_wiki_revisions (page_id, created_at DESC);

CREATE INDEX astrocyte_wiki_sources_memory_idx
  ON astrocyte_wiki_revision_sources (bank_id, memory_id);

CREATE INDEX astrocyte_wiki_lint_open_idx
  ON astrocyte_wiki_lint_issues (page_id, status)
  WHERE status = 'open';
```

#### 4.1.2 Projection model

The current SQL revision is projected into pgvector as:

```text
id = "wiki:{page_id}:{revision_id}"
bank_id = same bank
text = current revision markdown
memory_layer = "compiled"
fact_type = "wiki"
metadata.page_id = page id
metadata.revision_id = revision id
metadata.slug = page slug
```

AGE receives only the traversal projection:

```text
(:WikiPage {page_id, bank_id, slug, title, kind, current_revision_id, confidence})
(:Memory {memory_id, bank_id})
(:Entity {entity_id, bank_id, name, entity_type})

(:WikiPage)-[:CITES {revision_id, quote, relevance}]->(:Memory)
(:WikiPage)-[:MENTIONS]->(:Entity)
(:WikiPage)-[:RELATED_TO {link_type}]->(:WikiPage)
(:Entity)-[:ALIAS_OF {evidence, confidence}]->(:Entity)
```

SQL owns truth; pgvector owns similarity; AGE owns traversal.

---

## 5. Hybrid topic-page picking

Purely rule-based routing (MIP) misses emergent topics; purely LLM-driven discovery drifts and costs too much. Hybrid:

1. **Mechanical first.** MIP rules map raw memory → candidate pages by tags, metadata, entity extraction (named entities → `entity:*` pages).
2. **LLM escalation.** If no rule matches, or the LLM disagrees with the rule result with high confidence, it proposes a new page or alternate placement.
3. **Page creation policy.** A new page is only created if (a) ≥ N raw memories would attach to it, or (b) the user explicitly seeds it. Prevents page sprawl.

This mirrors MIP's own "declarative routing with LLM intent escalation" design — one mental model for the whole system.

---

## 6. Recall with wiki tiering

Today: `recall()` returns ranked raw hits.

With wiki: recall runs a **two-tier search**:

```
query_vector = embed(query)
compiled_hits = vector_store.search(memory_layer="compiled")
raw_hits = vector_store.search(raw memories)
lexical_hits = document_store.search_fulltext(query)
hydrated_pages = sql.fetch_pages(compiled_hits.page_ids)
graph_expansion = age.expand(top hydrated pages/entities, bounded)
return rrf(compiled_hits, raw_hits, lexical_hits, graph_expansion)
```

**Always return provenance.** A wiki page hit carries its `source_ids`, so callers can trace a claim back to the raw memory — and `reflect()` can still stitch raw detail when needed.

Normal `recall()` should not start with AGE traversal or SQL page scans. Start with indexed retrieval, then batch-hydrate SQL and run bounded AGE expansion only for the top candidates. `reflect()` may request deeper expansion because it is already a higher-latency operation.

---

## 7. Lint pass

Periodic (async, cron-scheduled) pass over wiki pages:

| Check | Action |
|---|---|
| Contradictions between pages | Flag on both pages; surface in audit log; optionally call LLM to resolve |
| Stale claims (page unchanged but underlying raw memories forgotten) | Mark for recompile |
| Orphaned pages (no incoming cross-links, no recall hits in N days) | Candidate for archival per `memory-lifecycle.md` §2 |
| Missing cross-links (entity mentioned but not linked) | LLM fills in |

Lint is the piece LongMemEval's **`knowledge-update`** category directly exercises — a memory system that silently keeps a stale fact alongside its correction fails this category. Lint is how we pass it.

---

## 8. Configuration (opt-in per bank)

```yaml
# astrocyte.yaml — per-bank compile config
banks:
  user-alice:
    compile:
      enabled: true
      page_kinds: ["entity", "topic"]         # Subset; "concept" is more expensive
      min_memories_to_create_page: 3

      trigger:
        # Automatic threshold — either condition fires a compile job
        size_threshold: 50                    # New memories since last compile
        staleness_days: 7                     # Days since last compile (paired with…)
        staleness_min_memories: 10            # …minimum new memories for staleness to trigger

      clustering:
        # DBSCAN parameters for untagged memory scope discovery (§3.2)
        eps: 0.25                             # Cosine distance threshold for neighborhood
        min_samples: 5                        # Minimum memories to form a cluster
        noise_holdover: true                  # Hold noise points for next compile cycle

      schedule:
        compile_queue: async                  # "async" | "inline" (inline = debug only)
        lint: "0 4 * * *"                    # Cron; daily 4am

      wiki_confidence_threshold: 0.7         # §6 tier fallback
      llm:
        compile_model: openai/gpt-4o-mini     # Cheap model for merge + cluster labelling
        lint_model: openai/gpt-4o             # Stronger for contradictions
```

Defaults: **disabled**. Banks that don't opt in keep today's behaviour exactly.

---

## 9. Evaluation — compile vs no-compile

We must be able to **measure whether compile actually helps**, per bank and per benchmark. The LongMemEval harness (`astrocyte-py/astrocyte/eval/benchmarks/longmemeval.py`) already gives us the categories that should move:

| Category | Expected impact | Why |
|---|---|---|
| `extraction` | Neutral to small + | Single-session answer already in raw |
| `multi-session` (reasoning) | **Large +** | Cross-session fusion is exactly what a topic page stores |
| `temporal` | Small + | Page revisions carry ordering |
| `knowledge-update` | **Large +** | Lint resolves stale-vs-current |
| `abstention` | Neutral | Orthogonal |

### 9.1 Harness changes

- Add `--compile on|off` flag to `scripts/run_benchmarks.py` (pairs with the new `BenchmarkRunOutcome` dataclass).
- Run both modes in CI on every release candidate; store results side-by-side.
- Ship a **regression gate**: `knowledge-update` and `multi-session` accuracy must be **≥ no-compile baseline**. Any regression blocks the release.
- Track cost: tokens and wall-time per compile call, amortised over recall hits.

### 9.2 Success criteria for GA validation

- `multi-session` or `knowledge-update` improves ≥ **10 points absolute** on LongMemEval
- No other category regresses > 2 points
- Compile cost per retain ≤ **2× baseline retain cost** on the `openai/gpt-4o-mini` tier
- Retain p95 latency unchanged (async queue; compile is off the hot path)

If these don't land, compile stays opt-in experimental and we don't wire it into default configs.

---

## 10. Explicit API

```python
# Explicit compile with scope — scope acts as both filter and wiki page label
await brain.compile("incident response", bank_id="eng-team")

# Explicit compile without scope — triggers full scope discovery (§3.2)
await brain.compile(bank_id="eng-team")
```

Both forms are async and return a `CompileResult`:

```python
@dataclass
class CompileResult:
    bank_id: str
    scopes_compiled: list[str]       # Scope strings that produced wiki pages
    pages_created: int
    pages_updated: int
    noise_memories: int              # Untagged memories held for next cycle (DBSCAN noise)
    tokens_used: int
    elapsed_ms: int
```

---

## 11. Open questions

- **Page granularity.** Is `entity:alice` one page, or one-per-bank? Cross-bank entity consolidation is a governance minefield — defer.
- **Write amplification budget.** A single retain can touch many pages. Do we cap pages-touched-per-retain, or cap tokens-per-hour per bank?
- **Recompile-on-forget storm.** Forgetting a widely-cited source can trigger mass recompile. Throttle or batch?
- **Multi-actor contention.** Two retains hitting the same page concurrently — last-write-wins, CRDT, or queue-serialised per page? Queue-per-page is simplest.
- **Does wiki obsolete `reflect()`?** Probably not — `reflect()` still composes across pages at query time. But the two need a clear division of labour.
- **Schema drift across models.** Compile output depends on the model. Version the compile prompt and re-run on major model swaps.
- **DBSCAN parameter tuning.** The right `eps` and `min_samples` values depend on the embedding model and typical memory length. Start with L2-normalized embeddings + cosine distance, grid-search `eps` in ~`0.08–0.30` and `min_samples` in `3–10` on LongMemEval, then pick the **smallest** `eps` that keeps noise in a target band (e.g. `10–35%`) while avoiding giant catch-all clusters (e.g. largest cluster `<40%` of points). Re-run calibration whenever the embedding model changes.
- **Noise holdover accumulation.** If a memory is noise for many consecutive compile cycles (no cluster forms around it), should it eventually be force-assigned to the nearest cluster, or flagged for operator review?

---

## 12. Relationship to existing Astrocyte components

| Component | Relationship |
|---|---|
| `memory-lifecycle.md` § Consolidation | Compile is a **superset** of consolidation — it produces the compiled output consolidation hinted at. Consolidation stays a no-op for banks without compile enabled. |
| `memory-intent-protocol.md` | MIP routing picks candidate pages in §5. Compile extends MIP, does not replace it. |
| `reflect()` | Still answers cross-page queries at recall time. Unchanged SPI. |
| `innovations.md` | Add "LLM wiki compile" to the innovations list once shipped. |
| `evaluation.md` | Wire the compile on/off A/B into the standard eval run. |

---

## 13. Delivery plan (v0.9.x)

Rough sequencing (each numbered item is a candidate milestone for roadmap):

1. **W1 — WikiPage memory kind + storage.** Rewritable, versioned, provenance. No compile yet; just the type and its persistence.
2. **W2 — CompileEngine MVP.** Inline (for debugging), single page kind (`entity`), explicit `brain.compile(scope, bank_id)` only.
3. **W3 — Scope discovery.** Tag grouping + DBSCAN clustering (§3.2) for automatic scope resolution.
4. **W4 — Threshold trigger + async queue + per-bank opt-in config.** Wire MIP config, move off inline.
5. **W5 — Recall wiki-tier precedence.** §6 two-tier search with threshold.
6. **W6 — Lint pass.** Contradictions, stale, orphans. Scheduled cron.
7. **W7 — Evaluation harness A/B + regression gate.** §9. Ship only if gate passes.
8. **W8 — Deferred:** cross-bank entity pages, concept pages, recompile-on-forget storm controls.

Each step has a clean rollback: disable compile in config, wiki tier falls back to raw RAG.

---

## 14. Non-goals (explicit)

- **Not a documentation system.** Wiki pages are internal memory artefacts, not user-facing markdown docs.
- **Not a replacement for raw memory.** Raw memories remain the source of truth; pages are derived.
- **Not a graph database.** Cross-links are soft references, not a formal graph ontology. Graph store remains orthogonal.
- **Not shipped on by default.** Opt-in until the evaluation gate proves it wins consistently.
