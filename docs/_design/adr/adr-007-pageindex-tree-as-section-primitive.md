# ADR-007: PageIndex tree section as the recall primitive

## Status

Accepted — implemented in M9 (`recall.md` §4.2, §8.1).

## Context

Astrocyte M1–M8 stores knowledge as **atomized memory units**: each call to `retain()` produces N facts, each is embedded and stored as a row in `memory_units`. Recall retrieves and ranks these atomized facts.

This shape is good for proof_count (multiple sessions confirming the same fact), lifecycle (forget-by-fact), and lint operations. It is **bad for recall accuracy** in three measurable ways:

1. **Conversation structure is destroyed**. A multi-hop question over Jon (mentioned in session 5) and Gina (mentioned in session 8) needs to know which session each fact came from. Atomized facts strip session boundaries — everything is a flat ranked list.
2. **Synth context budget is wasted**. 12 atomized memory units are 12 disjoint snippets, each carrying speaker/dia_id/timestamp metadata redundantly (~3000 tokens). 12 sections of the same conversation are 12 contiguous excerpts (~1500 tokens) that preserve discourse flow.
3. **Picker has nothing to navigate**. PageIndex's two-step pick→synth pattern requires a hierarchical structure with addressable anchors. Atomized facts have no hierarchy.

Phase A POC validated the alternative: build a [PageIndex](https://github.com/VectifyAI/PageIndex) tree per conversation, make `(document_id, line_num)` the recall primitive, retrieve sections instead of facts. The POC reached 62% on LoCoMo with single-hop at 90% and adversarial at 100% — comparable to v0.13.0's 83.28% baseline despite running on 1/10 the retain infrastructure.

## Decision

**Sections become the primary recall primitive.** Specifically:

1. New table `pageindex_sections (document_id, line_num, ...)` is the row that recall retrieves.
2. Recall strategies (semantic, keyword, entity, temporal, graph) all return ranked lists of `(document_id, line_num)` tuples.
3. The PageIndex picker, RRF fusion, cross-encoder rerank, and synth all operate at section grain.
4. Section excerpts (sliced from `pageindex_documents.md_text` by `line_num`) are what the synth sees.

`memory_units` (Layer 3) **stays** for:
- proof_count and confidence scoring at write time
- lifecycle operations (forget, archive, lint of raw facts)
- The optional verbatim verification fetch when synth wants to check a wiki claim against original source

`memory_units` is **off the hot recall read path**. Recall does not query `memory_units` for top-K retrieval.

## Alternatives considered

### Alt 1: Keep memory_units as the primitive; layer sections on top
Treat sections as a derived view of memory_units; recall still queries memory_units but groups by document/session.

**Rejected** because:
- Doesn't fix the synth context-budget issue (recall still returns atomized facts, then the synth reconstructs sections at read time).
- Doesn't give the picker an indexable navigation structure — the tree summaries are redundant if the underlying retrieval is fact-grain.
- Two storage models duplicate work at retain time.

### Alt 2: Both grains as first-class (multi-grain retrieval)
Retrieve at both grains, fuse the results.

**Rejected** because:
- Per-grain results aren't directly comparable (RRF works on rank, not on grain mismatch).
- Doubles the index footprint and retain cost.
- Phase A demonstrated that section grain alone clears the bench targets we set.

### Alt 3: Sections only; eliminate memory_units
Drop `memory_units` entirely. Sections are everything.

**Rejected** because:
- proof_count is a useful signal that doesn't map cleanly to sections (a fact can be confirmed across many sections; section-level proof_count would conflate that with section-level repetition).
- Lifecycle operations (forget specific facts) are easier at fact grain.
- Existing Astrocyte deployments and APIs depend on memory_units; removing them is a much bigger break than removing AGE.

## Consequences

### Positive
- Recall accuracy gains from preserving conversation structure (validated in Phase A: single-hop 90%, adversarial 100%).
- Synth context budget halved for the same number of "hits" — better answers from cheaper synth calls.
- PageIndex picker has a navigable structure to operate over (sections nest in a tree).
- `wiki_section_provenance` gives a clean cross-layer link: wiki page → sections → raw memory_units when verification is needed.

### Negative
- Two storage models (sections + memory_units) at retain time — slightly higher write cost.
- Schema migration to add `pageindex_documents` and `pageindex_sections`.
- Recall code is rewritten around section-grain results; M1–M8's memory-unit-grain recall API stays for back-compat.

### Operational
- `RecallResult.hits` returns section-grain `SectionHit` shape.  The legacy memory-unit-grain recall path (M1-M8) is removed; callers that need fact-grain access for `forget` / lifecycle operations use the dedicated lifecycle API, not `recall()`.

## See also

- [recall.md](../recall.md) — §4.2 schema, §8.1 design rationale
- [adr-006-three-layer-recall-stack.md](adr-006-three-layer-recall-stack.md) — three-layer architecture
- [memory-lifecycle.md](../memory-lifecycle.md) — lifecycle operations stay at memory_unit grain

## References

- Phase A POC: `astrocyte-py/scripts/bench_pageindex_locomo.py` (v7, 62% LoCoMo)
- [PageIndex framework intro](https://pageindex.ai/blog/pageindex-intro)
