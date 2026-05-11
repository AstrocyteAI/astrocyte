# Benchmark comparison methodology

How to compare Astrocyte's LongMemEval / LoCoMo scores against other memory systems honestly. Maintained as a reference because vendor numbers in this space are dominated by harness-and-judge variance, not by system quality.

---

## 1. What a "harness" is

A **benchmark harness** is the eval scaffolding around a dataset — everything that isn't the questions and gold answers themselves:

1. **Ingestion** — how the conversation/document is fed in. One big dump? Session-by-session? Are speaker labels preserved? Are timestamps surfaced?
2. **Retrieval / answering** — when a question arrives, how much context is pulled, what tools or agent loop wraps the memory, and which model generates the final answer.
3. **Judging** — which LLM grades the answer, with what prompt, against what gold answer.

A **vendor-built harness** is one a memory company wrote themselves to run a public benchmark on their own system. Examples in current use:

- **Mem0's `memory-benchmarks` repo** — Mem0's scripts that ingest LoCoMo/LongMemEval, query Mem0, judge with their prompt.
- **Hindsight's AMB (Agent Memory Benchmark)** — Hindsight's framework, runs LongMemEval-S and a LoCoMo10 subset through their judge config.
- **SuperMemory's `memorybench`** — same pattern.
- **Zep's harness** in their SOTA blog post.

Contrast with the **original-paper harness** — the eval code released alongside the LongMemEval or LoCoMo dataset by its authors.

---

## 2. Why direct comparison breaks

Even with the same dataset, harnesses differ on five axes that each move scores by single-digit-to-double-digit percentage points:

| Axis | Observed swing |
|---|---|
| **Judge model** | SuperMemory: 81.6% (gpt-4o) → 85.2% (gemini-3-pro-preview) on the same system. Mastra: 84.23 → 93.27 on the same swap. |
| **Judge prompt** | Mem0's judge vs the LongMemEval paper's judge score the same answer differently — strictness differs. |
| **Ingestion shape** | Session boundaries, speaker labels, and timestamps presence/absence shifts what the system can retrieve. |
| **Agent loop** | One-shot retrieval vs multi-turn ReAct vs filesystem-agent (Letta's 74% LoCoMo uses gpt-4o-mini in a filesystem-agent loop). |
| **Subset** | Hindsight reports on LoCoMo10 (10 conversations); Mem0 on full 1540 questions; SuperMemory skips LoCoMo entirely. |

**Concrete proof of harness-dominance:** Zep on LoCoMo measures at **58.44% (Mem0 re-run) / 75.14% (Zep corrected re-run) / 84% (Zep original claim)** — same system, three harnesses, 26pp spread.

Vendor numbers in the 80s and 90s come almost exclusively from vendor-built harnesses. Original-paper-harness numbers cluster much lower (full-context gpt-4o baseline is 60-64% on LongMemEval, 63.79 single-hop J on LoCoMo).

---

## 3. Three tiers of honest comparison

### Tier 1 — Matched harness (gold standard)

Pick one harness, run every system through it. This is what Mem0 did in their ECAI'25 paper Table 1: their harness, re-running LangMem, Zep, A-Mem, OpenAI Memory, and full-context baseline. Every cell judged by the same gpt-4o prompt against the same gold answers.

**For Astrocyte:** clone `mem0ai/memory-benchmarks`, write a thin Astrocyte adapter (ingest, query, reset), re-run. Result is directly comparable to Mem0's published 67.13 single-hop on LoCoMo.

- **Cost:** ~$50-200 in LLM calls per benchmark; ~1-2 days of adapter work.
- **Required for:** any public head-to-head claim.

### Tier 2 — Triangulate against shared anchors (defensible)

Compare *relative to a common baseline that appears in multiple harnesses*. The **full-context gpt-4o baseline** is the universal anchor — nearly every system reports it:

- LongMemEval paper: full-context gpt-4o = 60.6-64%
- Mem0 paper: full-context = 63.79 (single-hop LoCoMo J)
- Zep's harness: full-context beats Mem0 on LoCoMo
- Hindsight's AMB: full-context "scores similarly to brute-force context-stuffer"

Instead of "Astrocyte 53.5 vs Mem0 93.4" (different harnesses), frame as deltas-vs-baseline: **"Astrocyte sits ~10pp below full-context on LME; Mem0 claims +30pp above full-context on their harness."**

Most apples-to-oranges distortion gets caught because a harness that inflates the system-under-test usually inflates its baseline by a similar amount.

- **Required for:** internal slides, design discussions, blog posts that name competitors.

### Tier 3 — Cite ranges with provenance (minimum bar)

If you can't run a matched harness and there's no shared anchor, publish ranges, not points:

> Zep on LoCoMo: 58-84% across three harnesses (Mem0 re-run / Zep's corrected re-run / Zep's original claim).

Footnote which number came from which harness so the reader can apply their own judgment.

- **Required for:** internal notes. Not strong enough for external publication.

---

## 4. Astrocyte's reference table

### Direct peers (Mem0 ECAI'25 Table 1, gpt-4o judge, matched harness)

LoCoMo single-hop J scores, all run through Mem0's harness:

| System | LoCoMo J (single-hop) |
|---|---|
| A-Mem | 39.79 |
| MemoryBank (LME-S full task) | 26% |
| Zep (Mem0 re-run) | 58.44 / 61.70 |
| LangMem (Mem0 re-run) | 62.23 |
| OpenAI Memory | 63.79 |
| **Astrocyte M12.1 (full LoCoMo)** | **62.0%** |
| Mem0 (paper config) | 66.93 |
| Mem0-g (paper config) | 68.40 |

### Vendor self-reports (different harnesses — treat as ceilings, not peers)

| System | LME | LoCoMo | Harness |
|---|---|---|---|
| Mem0 | 93.4% | 91.6% | `mem0ai/memory-benchmarks`, gpt-4o judge |
| Hindsight | 94.6% | 92.0% | AMB, LoCoMo10 subset, judge undisclosed |
| SuperMemory | 81.6% (gpt-4o) / 85.2% (gemini-3-pro) | not published | own `memorybench` |
| Zep | 71.2% / 60.2% | 75.14% (Zep re-run) | own harness |
| Letta filesystem agent | — | 74.0% | own harness, gpt-4o-mini answer model |

---

## 5. Operating rules for Astrocyte comparisons

1. **Never stack Astrocyte's number next to a vendor self-report without flagging the harness mismatch.** That's narrative, not comparison.
2. **Always state our harness config** when publishing a number: dataset (LongMemEval-S full 500 / LoCoMo full 1540 / subset?), judge model (gpt-4o vs gpt-4o-mini), answer model, agent loop.
3. **For external publication, target Tier 1.** Run Mem0's open harness against Astrocyte before any public head-to-head claim. The matched-harness Mem0-paper Table 1 row is the credibility ceiling we should aim for.
4. **For internal slides, Tier 2 is enough.** Frame deltas vs the full-context gpt-4o baseline — it's the only number everyone reports.
5. **Watch the LoCoMo subset trap.** Hindsight's 92% is on LoCoMo10 (10 conversations); Mem0's 91.6% is on the full 1540 questions. Same dataset name, different difficulty.

---

## 6. Open gaps to close

- **Hindsight's AMB judge model** — not disclosed in the public blog. Their arxiv 2512.12818 likely specifies it.
- **Mem0-harness Astrocyte number** — not yet run. Highest-leverage Tier 1 work because Mem0 has the most competitor coverage in their Table 1.
- **AMB-harness Astrocyte number** — also valuable because Hindsight is our closest architectural reference, but requires running through their framework.
- **Cognee on LME/LoCoMo** — Cognee hasn't published these numbers themselves. Would have to be self-run on their open harness if we want a head-to-head.

---

## 7. Sources

- [LongMemEval paper (arxiv 2410.10813)](https://arxiv.org/pdf/2410.10813)
- [LoCoMo project page (Snap Research)](https://snap-research.github.io/locomo/)
- [Mem0 ECAI 2025 paper (arxiv 2504.19413)](https://arxiv.org/html/2504.19413v1)
- [Mem0 `memory-benchmarks` repo](https://github.com/mem0ai/memory-benchmarks)
- [Zep — State of the Art in Agent Memory](https://blog.getzep.com/state-of-the-art-agent-memory/)
- [Zep — "Is Mem0 Really SOTA" LoCoMo dispute](https://blog.getzep.com/lies-damn-lies-statistics-is-mem0-really-sota-in-agent-memory/)
- [SuperMemory research](https://supermemory.ai/research/)
- [Hindsight — Agent Memory Benchmark blog](https://hindsight.vectorize.io/blog/2026/03/23/agent-memory-benchmark)
- [Letta — Benchmarking AI Agent Memory](https://www.letta.com/blog/benchmarking-ai-agent-memory)
