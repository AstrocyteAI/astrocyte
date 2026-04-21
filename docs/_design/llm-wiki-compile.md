# LLM wiki compile — compounding knowledge pages for Astrocyte

**Status:** Draft design note (exploratory). Targeted for the **v0.9-era** feature wave (tagged on the `v0.8.x` train; see `product-roadmap-v1.md` § Release numbering).

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
[enqueue compile job]  ← async, per-bank queue
    ↓
CompileEngine.compile(raw_memory):
    1. topic_pick(raw_memory) → list[WikiPage]    (hybrid: MIP rules + LLM)
    2. for each page:
         merge(page, raw_memory) → new page revision
         write(page_revision)  ← rewritable; retains revision history
    3. cross-link(pages_touched)                   (entity → entity edges)
    4. emit memory.compiled event
```

**Async, never blocks retain.** Retain latency must stay bounded; compile is eventual. Failures are retried and observable but do not fail the write path.

**Per-bank opt-in.** Compile is expensive (LLM cost × pages-touched × raw-memories). Turn it on per bank via MIP config; off by default.

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

- Live revision: hot storage alongside raw memories (vector + document)
- Past revisions: cold / audit log (for lint and debugging; not indexed for recall)
- Forget semantics: forgetting a raw source triggers **recompile** of the pages that cited it — not tombstone-then-drift

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
hits = search_wiki_pages(query)
if hits and hits.top_score >= WIKI_CONFIDENCE_THRESHOLD:
    return hits + citations_to_raw(hits)
else:
    return search_raw(query)        # fall back to today's RAG path
```

**Always return provenance.** A wiki page hit carries its `source_ids`, so callers can trace a claim back to the raw memory — and `reflect()` can still stitch raw detail when needed.

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
      schedule:
        compile_queue: async                   # "async" | "inline" (inline = debug only)
        lint: "0 4 * * *"                     # Cron; daily 4am
      wiki_confidence_threshold: 0.7          # §6 tier fallback
      llm:
        compile_model: openai/gpt-4o-mini      # Cheap model for merge ops
        lint_model: openai/gpt-4o              # Stronger for contradictions
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

### 9.2 Success criteria for v0.9-era shipping

- `multi-session` or `knowledge-update` improves ≥ **10 points absolute** on LongMemEval
- No other category regresses > 2 points
- Compile cost per retain ≤ **2× baseline retain cost** on the `openai/gpt-4o-mini` tier
- Retain p95 latency unchanged (async queue; compile is off the hot path)

If these don't land, compile stays opt-in experimental and we don't wire it into default configs.

---

## 10. Open questions

- **Page granularity.** Is `entity:alice` one page, or one-per-bank? Cross-bank entity consolidation is a governance minefield — defer.
- **Write amplification budget.** A single retain can touch many pages. Do we cap pages-touched-per-retain, or cap tokens-per-hour per bank?
- **Recompile-on-forget storm.** Forgetting a widely-cited source can trigger mass recompile. Throttle or batch?
- **Multi-actor contention.** Two retains hitting the same page concurrently — last-write-wins, CRDT, or queue-serialised per page? Queue-per-page is simplest.
- **Does wiki obsolete `reflect()`?** Probably not — `reflect()` still composes across pages at query time. But the two need a clear division of labour.
- **Schema drift across models.** Compile output depends on the model. Version the compile prompt and re-run on major model swaps.

---

## 11. Relationship to existing Astrocyte components

| Component | Relationship |
|---|---|
| `memory-lifecycle.md` § Consolidation | Compile is a **superset** of consolidation — it produces the compiled output consolidation hinted at. Consolidation stays a no-op for banks without compile enabled. |
| `memory-intent-protocol.md` | MIP routing picks candidate pages in §5. Compile extends MIP, does not replace it. |
| `reflect()` | Still answers cross-page queries at recall time. Unchanged SPI. |
| `innovations.md` | Add "LLM wiki compile" to the innovations list once shipped. |
| `evaluation.md` | Wire the compile on/off A/B into the standard eval run. |

---

## 12. Delivery plan (v0.9-era)

**Release numbering note:** per `product-roadmap-v1.md`, the team ships this wave on `v0.8.x` tags, not a semver `v0.9.0` tag. When this doc says "v0.9-era" it means the feature wave, not the git tag.

Rough sequencing (each numbered item is a candidate milestone for roadmap):

1. **W1 — WikiPage memory kind + storage.** Rewritable, versioned, provenance. No compile yet; just the type and its persistence.
2. **W2 — CompileEngine MVP.** Inline (for debugging), single page kind (`entity`), hybrid topic pick.
3. **W3 — Async queue + per-bank opt-in config.** Wire MIP config, move off inline.
4. **W4 — Recall wiki-tier precedence.** §6 two-tier search with threshold.
5. **W5 — Lint pass.** Contradictions, stale, orphans. Scheduled cron.
6. **W6 — Evaluation harness A/B + regression gate.** §9. Ship only if gate passes.
7. **W7 — Deferred:** cross-bank entity pages, concept pages, recompile-on-forget storm controls.

Each step has a clean rollback: disable compile in config, wiki tier falls back to raw RAG.

---

## 13. Non-goals (explicit)

- **Not a documentation system.** Wiki pages are internal memory artefacts, not user-facing markdown docs.
- **Not a replacement for raw memory.** Raw memories remain the source of truth; pages are derived.
- **Not a graph database.** Cross-links are soft references, not a formal graph ontology. Graph store remains orthogonal.
- **Not shipped on by default in v0.9-era.** Opt-in until the evaluation gate proves it wins consistently.
