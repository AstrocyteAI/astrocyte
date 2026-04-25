# Benchmark Roadmap

> **Status**: Living document. Updated 2026-04-25.
> Current adapters: LoCoMo ✅ LongMemEval ✅ | Planned: AMA-Bench · MemoryArena

---

## 1. Why three tiers of benchmarks

Each generation of memory benchmarks tests a fundamentally different capability. Running only LoCoMo and LongMemEval is like validating a car's suspension on a parking lot — necessary, but it doesn't tell you how it handles a mountain road.

| Tier | What it tests | Current status |
|---|---|---|
| **Tier 1 — Passive QA recall** | Can the memory system retrieve the right fact from a pre-loaded history? | LoCoMo ✅ LongMemEval ✅ |
| **Tier 2 — Agentic trajectory memory** | Can the memory system extract and retrieve from what an agent *did*, across heterogeneous task types and long horizons? | AMA-Bench — planned |
| **Tier 3 — Memory-driven task execution** | Does having better memory actually cause the agent to *succeed at tasks* across causally chained sessions? | MemoryArena — planned |

Tier 1 answers "does retrieval work?" Tier 2 answers "does memory work across real agent trajectories?" Tier 3 answers "does memory actually improve outcomes?"

A system can saturate Tier 1 and fail Tier 3 entirely — Mem0 scores ~90% on LoCoMo LLM-judge and 0.14 SR on MemoryArena. That gap is the most important story in memory system evaluation right now.

---

## 2. Current state — Tier 1

Both adapters are in `astrocyte/eval/benchmarks/`. They share a common pattern:
- **Retain phase**: ingest conversation sessions into a named bank
- **Eval phase**: recall + reflect per question, score with canonical judge
- **Checkpoint/resume**: `--resume` / `RESUME=1` for interrupted runs
- **Parallel execution**: both run concurrently via `asyncio.gather()` in `bench-full`

### Scoring conventions (important for competitor comparisons)

| Benchmark | Internal scorer | Canonical scorer | Cross-competitor scorer |
|---|---|---|---|
| LoCoMo | `word_overlap_score > 0.3` | Stemmed token-F1 (`--canonical-judge`) | LLM-judge yes/no (`--canonical-judge` + real provider) |
| LongMemEval | `text_overlap_score > 0.3` | LLM-judge (`--canonical-judge`) | Same |

Use `doppler run -- make bench-full` for competitor-comparable numbers.

### Competitor numbers (canonical LLM-judge, as of 2026-04-25)

**LoCoMo** (LLM-judge, from MemMachine's independent evaluation):

| System | Overall | Single-hop | Temporal | Multi-hop |
|---|---|---|---|---|
| MemMachine | 84.9% | 93.3% | 72.6% | 80.5% |
| Memobase | 75.8% | — | — | — |
| Zep | 75.1% | 74.1% | 79.8% | 66.0% |
| Mem0 | 66.9% | 67.1% | 55.5% | 51.2% |
| **Astrocyte** | *pending real run* | — | — | — |

**LongMemEval** (LLM-judge):

| System | Overall |
|---|---|
| Mastra | 94.9% |
| MemMachine | 93.0% |
| Emergence | 86.0% |
| Supermemory | 81.6% |
| Zep | 71.2% |
| **Astrocyte** | *pending real run* |

> **Note on score credibility**: Vendor self-reports on LoCoMo vary wildly due to differing evaluation setups (the Zep/Mem0 dispute is documented at github.com/getzep/zep-papers/issues/5). The MemMachine third-party table above is the most trustworthy source — same evaluation run across all systems. Run `bench-full` to get independently verified Astrocyte numbers.

---

## 3. Planned — Tier 2: AMA-Bench

**Paper**: [arXiv:2602.22769](https://arxiv.org/abs/2602.22769) (ICLR 2026 workshop)
**Dataset**: `AMA-bench/AMA-bench` on HuggingFace (50.5 MB, single JSONL)
**Leaderboard**: https://huggingface.co/spaces/AMA-bench/AMA-bench-Leaderboard (live)
**License**: MIT

### What it tests that LoCoMo/LME don't

- **Real agent trajectories** — not dialogue transcripts. Episodes come from Text-to-SQL, web navigation, gaming, embodied AI (BabyAI), and software engineering (SWEBench). Memory must be extracted from action-observation sequences, not clean conversation turns.
- **Typed capability decomposition** — four question categories let you diagnose exactly which capability is failing:
  - **Type A — Recall**: direct retrieval ("what command did the agent run in step 3?")
  - **Type B — Causal Inference**: cause-effect across events ("why did the agent fail the SQL query?")
  - **Type C — State Updating**: tracking changes ("what was the file state after the third edit?")
  - **Type D — State Abstraction**: pattern/summary understanding ("what strategy did the agent use overall?")
- **Scalable synthetic horizons** — controlled 8K–128K token tiers for isolating degradation by trajectory length. Real-world max is ~997K tokens (Open World QA domain).
- **Code and embodied domains** — SWEBench and BabyAI are entirely absent from LoCoMo and LME.

### What LoCoMo/LME test that AMA-Bench doesn't

- **Multi-session memory chains** — AMA-Bench episodes are single trajectories. There is no cross-session dependency.
- **Conversational/social memory** — personal facts, preferences, relationships. AMA-Bench is task-trajectory focused.

### Leaderboard scores (Qwen3-32B judge, real-world subset)

| System | Avg Accuracy |
|---|---|
| AMA-Agent (paper's method) | 57.2% |
| MemoRAG | 46.1% |
| HippoRAG2 | 44.8% |
| Qwen3-Embedding (dense RAG) | 42.3% |
| BM25 | 34.4% |
| MemGPT | 33.0% |
| Mem0 | 21.0% |

### Implementation plan

**Adapter contract** (two Python functions, then everything else is the existing pattern):

```python
async def memory_construction(trajectory: str, task: str) -> None:
    """Retain a full agent trajectory into a named bank."""
    await brain.retain(trajectory, bank_id=bank_id, ...)

async def memory_retrieve(question: str) -> str:
    """Retrieve and reflect against a question."""
    result = await brain.reflect(question, bank_id=bank_id)
    return result.answer
```

**Harness**: The AMA-Bench repo provides `src/run.py` and `src/evaluate.py`. Our adapter plugs into `run.py` by implementing the two functions above. The judge is Qwen3-32B via vLLM — can substitute a cloud provider by swapping `configs/llm_judge.yaml`.

**New file**: `astrocyte/eval/benchmarks/amabench.py`
**New make target**: `bench-amabench` (fetch dataset from HuggingFace, run adapter, score)
**Dataset fetch**: `huggingface-cli download AMA-bench/AMA-bench --repo-type dataset --local-dir datasets/amabench`

**Key design decisions to resolve**:
1. Trajectory format — AMA-Bench episodes include action/observation pairs; need to decide how to chunk these (one memory per turn vs. one per episode segment).
2. Bank isolation — one bank per episode (clean slate) or shared bank across episodes? The benchmark assumes clean-per-episode.
3. Judge — Qwen3-32B locally or proxy through an OpenAI-compatible endpoint via LiteLLM.

**Estimated effort**: 2–3 days. The dataset loading and eval loop are well-defined; the main work is the trajectory-to-memory chunking strategy.

**Priority**: Implement after first real `bench-full` run establishes LoCoMo + LME baselines.

---

## 4. Planned — Tier 3: MemoryArena

**Paper**: [arXiv:2602.16313](https://arxiv.org/abs/2602.16313) (Stanford/UCSD/Princeton/MIT, Feb 2026)
**Dataset**: `ZexueHe/memoryarena` on HuggingFace
**Leaderboard**: None (paper-only scores, no submission endpoint)
**Harness**: No public evaluation code — must be reconstructed from the paper

### What it tests that AMA-Bench doesn't

- **Memory that drives consequential actions** — the test is task success, not QA accuracy. Remembering a dietary restriction only counts if the agent books the right restaurant 4 sessions later and the booking succeeds. AMA-Bench tests recall in isolation after the trajectory is complete.
- **Causally chained multi-session tasks** — 6 sessions for shopping, 5–9 for travel, 2–16 for search/formal reasoning. Failure in session 2 propagates to session 5. This is fundamentally different from any single-trajectory benchmark.
- **Constraint accumulation under real-world task pressure** — Group Travel Planning (270 tasks) builds preference profiles across participants joining sequentially; each new traveler adds constraints that conflict with prior ones.
- **Formal reasoning with accumulating derivations** — Sequential math/physics (60 expert-curated tasks) require building on derivation steps across sessions. No equivalent exists in any other benchmark.
- **Deterministic correctness** — no LLM in the evaluation loop. SR and PS are computed programmatically from task outcomes.

### What AMA-Bench tests that MemoryArena doesn't

- **Typed capability diagnosis** (A/B/C/D categories)
- **Scalable synthetic horizons** (8K–128K tiers)
- **Code/software engineering domain**
- **Embodied AI domain**
- **Pluggable memory interface** (no agent execution loop needed)

### Published scores (all systems under 0.25 SR)

| System | Avg SR | Web Shopping SR | Travel sPS |
|---|---|---|---|
| RAG (text-embedding-3-small) | 0.23 | 0.00 | — |
| Claude-Sonnet | 0.19 | 0.12 | — |
| BM25 | 0.19 | 0.00 | — |
| Letta | 0.15 | 0.00 | — |
| Mem0 | 0.14 | 0.00 | — |

All systems collapse at Web Shopping (requires exact product compatibility matching across sessions) and Travel Planning (SR/PS ≈ 0 for all). Progressive Search is the least-hard domain.

### Implementation plan

**This is a significant infrastructure build.** The benchmark requires:

1. **Agent execution loop per domain** — not just retain/recall. Astrocyte would serve as the memory backend for a Claude/GPT agent that actually executes tasks in each environment.

2. **Domain environments**:
   - Web Shopping: WebArena-style shopping environment (can use their hosted instance or spin up locally)
   - Progressive Search: search API integration (Bing/SerpAPI)
   - Group Travel: constraint-checking harness (custom, relatively simple)
   - Formal Reasoning: PDF parsing + derivation chain executor (most complex)

3. **Session state management** — the memory bank must survive across sessions within a task chain, then be wiped between task chains.

4. **Scoring infrastructure** — SR and PS computed per domain using domain-specific correctness checks.

**Approach**: Use Astrocyte as the memory backend for an agent SDK integration (Claude Managed Agents or Claude Code SDK). The agent calls `brain.retain()` after each session and `brain.recall()` at the start of each new session. This tests the exact use case the SDK integrations are built for.

**New file**: `astrocyte/eval/benchmarks/memoryarena.py`
**Dependencies**: WebArena instance, search API key, domain-specific task runners

**Estimated effort**: 2–3 weeks for a credible implementation covering all four domains. Start with Progressive Search (simplest domain, search API only) as a proof of concept, then expand.

**Priority**: Implement after AMA-Bench. Rationale: AMA-Bench validates that memory retrieval quality is good before spending 2–3 weeks building agent execution loops whose results depend on retrieval quality being high.

---

## 5. Capability coverage map

Which capabilities does each benchmark test?

| Capability | LoCoMo | LongMemEval | AMA-Bench | MemoryArena |
|---|:---:|:---:|:---:|:---:|
| Single-session fact recall | ✅ | ✅ | ✅ (Type A) | — |
| Multi-session reasoning | ✅ | ✅ | — | ✅ |
| Temporal ordering | ✅ | ✅ | ✅ (Type C) | — |
| Knowledge update / conflict | — | ✅ | — | ✅ |
| Abstention / hallucination check | ✅ | ✅ | — | — |
| Causal inference | — | — | ✅ (Type B) | — |
| State tracking over actions | — | — | ✅ (Type C) | ✅ |
| Summary / abstraction | — | — | ✅ (Type D) | — |
| Code / SE domain | — | — | ✅ | — |
| Embodied / robotic domain | — | — | ✅ | — |
| Memory-driven task execution | — | — | — | ✅ |
| Cross-session constraint accumulation | — | — | — | ✅ |
| Formal reasoning chains | — | — | — | ✅ |
| Long horizon (>100K tokens) | — | ✅ | ✅ | — |
| Deterministic scoring (no LLM judge) | — | — | — | ✅ |

---

## 6. Implementation order and rationale

```
1. doppler run -- make bench-full       [NOW]
   → First real competitor-comparable LoCoMo + LME numbers
   → Promote results to baselines-openai.json
   → Update platform-positioning.md competitor table

2. AMA-Bench adapter                    [NEXT, ~2-3 days]
   → Validates memory retrieval on real agent trajectories
   → Gets Astrocyte on the HuggingFace leaderboard
   → Typed A/B/C/D breakdown identifies retrieval weaknesses

3. MemoryArena adapter                  [AFTER, ~2-3 weeks]
   → Tests whether better memory → better task outcomes
   → Requires AMA-Bench score ≥ 50% to justify the infra investment
   → Natural showcase for SDK integrations (Claude Managed Agents)
```

---

## 7. References

| Resource | URL |
|---|---|
| AMA-Bench paper | https://arxiv.org/abs/2602.22769 |
| AMA-Bench GitHub | https://github.com/AMA-Bench/AMA-Bench |
| AMA-Bench dataset | https://huggingface.co/datasets/AMA-bench/AMA-bench |
| AMA-Bench leaderboard | https://huggingface.co/spaces/AMA-bench/AMA-bench-Leaderboard |
| MemoryArena paper | https://arxiv.org/abs/2602.16313 |
| MemoryArena website | https://memoryarena.github.io |
| MemoryArena dataset | https://huggingface.co/datasets/ZexueHe/memoryarena |
| LoCoMo paper | https://arxiv.org/abs/2406.07378 |
| LongMemEval paper | https://arxiv.org/abs/2410.10813 |
| MemMachine LoCoMo scores | https://memmachine.ai/blog/2025/09/memmachine-reaches-new-heights-on-locomo/ |
| Zep/Mem0 scoring dispute | https://github.com/getzep/zep-papers/issues/5 |
| ATANT benchmark critique | https://arxiv.org/abs/2604.10981 |
