# Multimodal Memory Benchmarks

> **Status**: Research survey. Last updated 2026-05-17.
> Companion to `research-multimodal-memory.md`. Covers benchmarks for Phase 2 (multimodal) evaluation.
> Text benchmarks (LoCoMo, LongMemEval, AMA-Bench, MemoryArena) are in `benchmark-roadmap.md`.

---

## 1. Landscape summary

As of mid-2026, there is no single canonical benchmark for multimodal memory — the equivalent of ImageNet for vision does not yet exist in this space. The field fragmented across modalities, memory types, and evaluation paradigms. This document surveys the most relevant options and recommends a benchmark stack for the multimodal research track.

**Key finding from the 2025–2026 survey**: the previously asserted gap ("no cross-modal retrieval benchmark exists") is no longer accurate. MultiHaystack (March 2026) and DeepImageSearch (February 2026) directly address cross-modal retrieval at scale.

---

## 2. Benchmark catalogue

### 2.1 Multimodal memory — direct relevance

| Benchmark | arXiv | Date | What it tests |
|---|---|---|---|
| **MemLens** | [2605.14906](https://arxiv.org/abs/2605.14906) | May 2026 | Multimodal multi-session memory: 789 questions, 5 memory abilities (information extraction, multi-session reasoning, temporal reasoning, knowledge update, answer refusal), 4 context lengths (32K–256K tokens), 27 LVLMs + 7 memory-augmented agents evaluated |
| **Mem-Gallery** | via OmniMem | 2026 | 1,711 QA pairs from 240 multimodal dialogues with 1,003 grounded images, 3,962 conversational rounds; 9 question categories including Visual Search, Timeline Learning, Temporal Reasoning, and Multi-Entity Reasoning |
| **OmniMem** | [2604.01007](https://arxiv.org/abs/2604.01007) | Apr 2026 | Lifelong multimodal agent memory system; evaluates on LoCoMo + Mem-Gallery — closest current competitor |
| **MemVerse** | [2512.03627](https://arxiv.org/abs/2512.03627) | Dec 2025 | Hierarchical episodic-semantic memory with multimodal knowledge graph; three memory types: core, semantic, episodic |
| **AMA-Bench** | [2602.22769](https://arxiv.org/abs/2602.22769) | Feb 2026 | Long-horizon agentic memory across Text-to-SQL, web nav, gaming, embodied AI; already planned in `benchmark-roadmap.md` |

### 2.2 Cross-modal retrieval

| Benchmark | arXiv | Date | What it tests |
|---|---|---|---|
| **MultiHaystack** | [2603.05697](https://arxiv.org/abs/2603.05697) | Mar 2026 | First benchmark designed for cross-modal retrieval and reasoning at scale: 46,000+ candidates across documents, images, and videos; 747 open but verifiable questions |
| **DeepImageSearch** | [2602.10809](https://arxiv.org/abs/2602.10809) | Feb 2026 | Image retrieval from visual histories via agentic multi-step reasoning; reformulates retrieval as autonomous exploration |
| **LoVR** | [2505.13928](https://arxiv.org/abs/2505.13928) | May 2025 | Long video retrieval in multimodal contexts; 467 long videos, 40,804+ fine-grained clips with high-quality captions |

### 2.3 Memory types and continual learning

| Benchmark | arXiv | Date | What it tests |
|---|---|---|---|
| **LMEB** | [2603.12572](https://arxiv.org/abs/2603.12572) | Mar 2026 | Long-horizon memory embedding benchmark spanning 4 types: Episodic, Dialogue, Semantic, Procedural — unified evaluation framework for embedding models |
| **MemoryBench** | [2510.17281](https://arxiv.org/abs/2510.17281) | Oct 2025 | Memory and continual learning in LLM systems; both retention quality and forgetting under distribution shift |
| **EPBench** | [2501.13121](https://arxiv.org/abs/2501.13121) | Jan 2025 | Episodic memory generation and evaluation for LLMs; structured event fields including temporal/spatial context, entities, descriptions |

### 2.4 Long-context multimodal (retrieval-adjacent)

| Benchmark | arXiv | Date | What it tests |
|---|---|---|---|
| **MM-BRIGHT** | [2601.09562](https://arxiv.org/abs/2601.09562) | Jan 2026 | Reasoning-intensive multimodal retrieval; 2,803 real-world queries, 29 domains, 4 tasks of increasing complexity |
| **MLDocRAG** | [2602.10271](https://arxiv.org/abs/2602.10271) | Feb 2026 | Multimodal long-context document RAG; Chunk-Query Graph for organizing multimodal content |

---

## 3. Capability coverage map

Which capabilities does each benchmark test, relative to the research goals in `research-multimodal-memory.md`:

| Capability | MemLens | Mem-Gallery | MultiHaystack | LMEB | MemoryBench | LoVR |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| Episodic recall | ✅ | ✅ | — | ✅ | ✅ | — |
| Semantic compression | ✅ | — | — | ✅ | ✅ | — |
| Multi-session reasoning | ✅ | ✅ | — | ✅ | — | — |
| Temporal reasoning | ✅ | ✅ | — | ✅ | — | — |
| Knowledge update | ✅ | — | — | — | ✅ | — |
| Cross-modal retrieval | — | ✅ (partial) | ✅ | — | — | — |
| Continual learning | — | — | — | — | ✅ | — |
| Video memory | — | — | — | — | — | ✅ |
| Scale (>40K candidates) | — | — | ✅ | — | — | ✅ |
| Procedural memory | — | — | — | ✅ | — | — |

---

## 4. Recommended benchmark stack

For the multimodal research track (Phase 2 of `research-multimodal-memory.md`):

```
Text memory (existing):       LoCoMo + LongMemEval
Multimodal memory:            MemLens + Mem-Gallery
Cross-modal retrieval:        MultiHaystack
Memory type coverage:         LMEB (Episodic + Semantic + Procedural)
Continual learning:           MemoryBench
```

This stack covers all three research goals (retrieval quality, compute efficiency, continual learning) and both memory types (episodic, semantic) without redundancy.

**Why not all benchmarks**: running the full catalogue would be compute-prohibitive and dilute the paper's narrative. The recommended stack provides maximum coverage with minimum overlap.

---

## 5. Competitor baselines to track

**OmniMem** ([2604.01007](https://arxiv.org/abs/2604.01007)) is the most important competitor to read in full. It evaluates on LoCoMo and Mem-Gallery — the same benchmarks we use for text and multimodal respectively — making it the direct comparison target reviewers will expect.

**MemLens** ([2605.14906](https://arxiv.org/abs/2605.14906)) was published May 14, 2026 (3 days before this document). It is the newest benchmark in the space and likely to be heavily cited by reviewers. Evaluating on MemLens is close to mandatory for a 2026/2027 submission.

---

## 6. Implementation priority

| Benchmark | Effort | Priority | Depends on |
|---|---|---|---|
| MemLens | Medium — structured QA format | High — newest, most cited | Phase 2 encoder |
| Mem-Gallery | Medium — multimodal dialogue format | High — OmniMem baseline | Phase 2 encoder |
| MultiHaystack | Medium — retrieval-only format | High — cross-modal claim | Phase 2 encoder |
| LMEB | Low — embedding model eval | Medium — memory type coverage | Probe experiment |
| MemoryBench | Low — LLM system eval | Medium — CL claim | Phase 1 |
| LoVR | High — video processing | Low — video-specific | Later |

Implement MemoryBench and LMEB during Phase 1 (text). Implement MemLens, Mem-Gallery, and MultiHaystack in Phase 2.

---

## 7. References

| Resource | URL |
|---|---|
| MemLens paper | https://arxiv.org/abs/2605.14906 |
| OmniMem paper | https://arxiv.org/abs/2604.01007 |
| MultiHaystack paper | https://arxiv.org/abs/2603.05697 |
| LMEB paper | https://arxiv.org/abs/2603.12572 |
| MemoryBench paper | https://arxiv.org/abs/2510.17281 |
| Mem-Gallery (via OmniMem) | https://arxiv.org/abs/2604.01007 |
| MemVerse paper | https://arxiv.org/abs/2512.03627 |
| DeepImageSearch paper | https://arxiv.org/abs/2602.10809 |
| LoVR paper | https://arxiv.org/abs/2505.13928 |
| MM-BRIGHT paper | https://arxiv.org/abs/2601.09562 |
| EPBench paper | https://arxiv.org/abs/2501.13121 |
| Text benchmark roadmap | `benchmark-roadmap.md` |
| Research direction | `research-multimodal-memory.md` |
