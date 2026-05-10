# ADR-006: Three-layer recall stack (Wiki + Tree+Graph + Raw)

## Status

Accepted — implemented in M9 (`recall.md`).

## Context

Astrocyte's M1–M8 recall path is RAG over atomized memory units with optional AGE-backed graph traversal. Two structural problems surfaced during 2026-04 → 2026-05 benchmark work:

1. A 33pp LoCoMo regression between v0.13.0 (83.28%) and HEAD (~50%) that a 6-layer config-knob bisect could not isolate.
2. 17h LME retain wallclock dominated by per-session SFE calls; the resulting accuracy did not justify the cost.

A vectorless POC ([PageIndex](https://github.com/VectifyAI/PageIndex)) reached 62% on LoCoMo with zero retain-time vector cost — proving that the recall surface (not the storage layer) was the limiting factor.

Three references each solve a different sub-problem:

| Reference | Solves | Granularity |
|---|---|---|
| PageIndex | "Where in this document does X appear?" | Section / tree node |
| Hindsight | "Which facts share entities/links with the seed?" | Atomized fact ↔ section |
| Karpathy LLM Wiki | "What does the system know about X overall?" | Topic / entity page |

## Decision

The recall path is built as a three-layer stack with wiki-tier-first precedence:

```
L1: wiki_pages (compounding synthesis, M8 + Karpathy lint)
L2: pageindex_sections + section_entities + section_links (PageIndex tree + Hindsight graph)
L3: memory_units (existing Astrocyte; off the hot recall path, kept for proof_count + lifecycle)
```

Recall fans out across L1 and L2 in parallel SQL queries, fuses via RRF (k=60), reranks with a cross-encoder, then runs the PageIndex picker as a final reranker over the top-15 candidates. The mode-specific synth uses both wiki context (high priority) and section excerpts (source of truth).

Cross-layer glue:
- `wiki_section_provenance` (wiki_page_id → document_id, line_num) — wiki claims verifiable against source sections
- L2 sections carry source provenance back to L3 memory_units (existing `source_id` chain extended to sections)

## Alternatives considered

### Alt 1: Single-tier vector search (status quo, fix the regression)
Continue with M1–M8 architecture; bisect the v0.13.0→HEAD regression more aggressively.

**Rejected** because:
- 6 layers of bisecting failed to isolate the regression.
- Even at v0.13.0's 83.28%, the architecture has no answer for LME's `knowledge-update` category (proven in M8 W7 evaluation).
- Doubling down on atomized facts loses conversation structure that multi-hop and temporal categories need.

### Alt 2: PageIndex-only (Phase A POC at production scale)
Ship the file-based POC as a recall_strategy; skip the graph and wiki layers.

**Rejected** because:
- Phase A v7 capped at 62% overall; the picker non-compliance issue (returning `[1]`) is structural to operating on raw 30-node skeletons.
- No answer for LME `knowledge-update` (no synthesis layer).
- No answer for LME multi-session at scale (no graph for entity bridging across long histories).

### Alt 3: Hindsight-only (port their stack verbatim)
Replicate Hindsight's link_expansion + RRF + rerank against memory_units.

**Rejected** because:
- Loses conversation/document structure (atomized facts only).
- No equivalent to LLM Wiki for LME `knowledge-update`.
- Doesn't leverage the structural insight from Phase A: section-grain retrieval gives the synth a more efficient context budget.

### Alt 4: Two layers (L2 + L3, skip wiki)
Ship the recall stack without the wiki integration. Defer L1 to a later milestone.

**Rejected** because:
- The wiki layer is the LME differentiator; LoCoMo doesn't need it but LME `knowledge-update` and `multi-session` do.
- M8 already shipped the wiki schema and CompileEngine; the marginal cost of integration is small (PR3).
- Sequencing L1 separately (PR3 after PR2's bench gate clears) lets us validate L1+L2 incrementally without delaying the L2 win.

## Consequences

### Positive
- LoCoMo and LME share one retrieval system. No bench-specific recall paths.
- Each layer is independently testable (per-strategy unit tests, integration tests at the fusion stage, bench tests end-to-end).
- The wiki layer naturally handles LME `knowledge-update` via the supersession chain — the category that no atomized-fact architecture handles cleanly.
- PageIndex picker becomes a reranker over a small (top-15) curated set; this fixes the v6 non-compliance failure mode observed in Phase A.
- Removing AGE (ADR-008) drops a Postgres extension dep and simplifies the deployment surface.

### Negative
- Larger surface area: 5 new tables (or 4 if `wiki_contradictions` is rolled into M8's existing wiki schema), parallel-strategy code paths, mode classifier prompt to tune.
- Retain cost per session is ~3-4 LLM calls (summary, entity extract, causal/supersedes detect) plus 1 embedding. Comparable to current SFE+entity+links retain but at a different grain.
- Wiki compile is async and can fall behind retain; PR3 must validate that recall accuracy degrades gracefully when the wiki layer is stale.

### Operational
- New deployments default to graph_store=postgres_flat (ADR-008). Existing AGE deployments rebuild from raw memory_units (no migration tool ships).
- The recall stack is the canonical recall path. The legacy memory-unit-grain recall path (M1-M8) is removed; the section-grain stack is what `brain.recall()` runs against.

## See also

- [recall.md](../recall.md) — full design
- [adr-007-pageindex-tree-as-section-primitive.md](adr-007-pageindex-tree-as-section-primitive.md) — section-grain decision
- [adr-008-section-graph-replaces-age.md](adr-008-section-graph-replaces-age.md) — AGE removal
- [llm-wiki-compile.md](../llm-wiki-compile.md) — Layer 1 origins (M8)
- [hindsight-informed-capabilities.md](../hindsight-informed-capabilities.md) — Layer 2 graph techniques

## References

- [PageIndex framework intro](https://pageindex.ai/blog/pageindex-intro) — vectorless reasoning-based RAG
- [Hindsight retrieval doc](https://github.com/Hindsight-AI/hindsight) — multi-strategy fusion, link expansion
- [Karpathy LLM wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) — compounding wiki pages
- Phase A POC: `astrocyte-py/scripts/bench_pageindex_locomo.py` (v7, 62% LoCoMo)
