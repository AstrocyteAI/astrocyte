# M30 — Retrieval parallelization + top_50 reporting shift

**Status:** SHIPPED in v0.15.0 (2026-05-19)
**Predecessors:** [M28-M29 activate + coreference](m28-m29-activate-and-coreference.md), [M27 memory quality](m27-memory-quality.md)
**Successor (queued):** M30b ingest-race investigation + answerer-call latency

---

## 1. Cycle goal

Two narrow, safe-by-construction parallelization wins on the bench retrieval path, motivated by Hindsight comparison (`hindsight-api-slim/.../retrieval.py:698-745` runs sibling retrieval branches via `asyncio.gather`). Hypothesis: cut per-question latency 1.5–2× by replacing sequential `await` chains with `asyncio.gather`.

Secondary goal that emerged mid-cycle: validate whether our reporting cutoff (top_200) is the right operating point for the gpt-4o-mini answerer.

## 2. Changes

### L1 — Parallel `fact_recall` + `section_recall` in bench client

**File:** `scripts/mem0_harness/astrocyte_client.py` (`AstrocyteClient.search`)

Before:
```python
fact_hits = await fact_recall(store=..., ...)        # ~1-2s
recall = await section_recall(store=..., ...)        # ~1-2s, independent
```

After:
```python
fact_coro = fact_recall(store=..., ...)
section_coro = section_recall(store=..., ...)
fact_result, section_result = await asyncio.gather(
    fact_coro, section_coro, return_exceptions=True,
)
# Per-branch exception handling preserved — a section_recall failure
# can't take fact_recall down and vice versa.
```

### L2 — Parallel observations + mental-model fetch in answerer wrapper

**File:** `scripts/mem0_harness/_hindsight_answerer.py` (`_wrapper`)

Before:
```python
observations = await fetch_obs(user_id, limit=20)         # ~0.3s
mental_models = await fetch_mm(user_id, limit=10)         # ~0.3s
```

After:
```python
import asyncio as _asyncio
gathered = await _asyncio.gather(
    fetch_obs(user_id, limit=20),
    fetch_mm(user_id, limit=10),
    return_exceptions=True,
)
```

Both changes are isolated to the bench harness; the production `Astrocyte.search()` API surface is unchanged.

## 3. Bench cycle — three runs, three lessons

### R1 (project=astrocyte-m30) — config error

Launched `make bench-mem0-harness-parallel MEM0_HARNESS_PROJECT=astrocyte-m30` without setting `MEM0_HARNESS_LOCOMO_MAX_Q=20`. The Makefile defaults to no sample limit, so LoCoMo started running on the **full 1500+ question dataset** (~15-hour run). Aborted ~25% in.

**Lesson:** the Makefile variable was set in the user's shell env for M28-M29; I didn't carry it forward when I launched the background bench. Document this in RELEASING.md or bake the default into the bench-mem0-harness-parallel target.

### R2 (project=astrocyte-m30, relaunch with proper sampling) — bench state pollution

Re-launched with `MEM0_HARNESS_LOCOMO_MAX_Q=20`. Result: LME 22/30 (parity ✓), LoCoMo 97/200 (−73q, looked catastrophic).

Root cause: the mem0 bench runner caches per-conversation ingestion state in `predicted_<project_name>/_ingestion_N.json` files. When R1 was killed mid-flight, it had written markers for convs 0-3. R2 launched with the same project name; the runner saw the markers and **skipped ingestion for convs 0-3** even though the DB had been freshly reset between runs. Those 80 questions retrieved zero memories.

Evidence in the log:
```
INFO - Conversation 0 already ingested (user_id=locomo_0_3cf4c678, 419 chunks)
INFO - Conversation 1 already ingested (user_id=locomo_1_3cf4c678, 369 chunks)
INFO - Conversation 2 already ingested (user_id=locomo_2_3cf4c678, 663 chunks)
INFO - Conversation 3 already ingested (user_id=locomo_3_3cf4c678, 629 chunks)
INFO - Ingesting conversation 4: ...
```

Verified by counting `[pageindex] indexed doc=...` events (6 of 10 — exactly the convs that were NOT skipped) and per-conv accuracy distribution (every question for convs 0-3 got zero memories; convs 4-9 retrieved normally).

**Lesson:** the bench runner's ingestion-marker cache is **independent of the database state**. When killing a bench mid-flight and relaunching:
- Either use a fresh `MEM0_HARNESS_PROJECT` name (e.g. `astrocyte-m30c` instead of `astrocyte-m30`)
- Or delete `benchmark-results/mem0_harness/{lme,locomo}/<project>/predicted_<project>/_ingestion_*.json` before relaunching

The deeper fix (DB-level marker verification, or auto-deletion of marker files when bench-runner-deps re-resets the DB) is queued as M30b-operational.

### R3 (project=astrocyte-m30c) — clean state, real measurement

Fresh project name, full re-ingestion, real comparison.

**LME @ top_200: 21/30 (70.0%)** — −1q vs M28-M29's 22/30. Judge noise at n=5 — single-session-preference dropped from 3 to 2 (40%); R2 same code on different judge run held 3 (60%).

**LoCoMo @ top_200: 166/200 (83.0%)** — −4q vs M28-M29's 170/200. Per-category:
| Category | M28-M29 | M30-R3 | Δ |
|---|---|---|---|
| multi-hop | 74/79 (93.7%) | 74/79 (93.7%) | 0 |
| open-domain | 25/31 (80.6%) | 25/31 (80.6%) | 0 |
| single-hop | 4/4 (100%) | 4/4 (100%) | 0 |
| temporal | 67/86 (77.9%) | 63/86 (73.3%) | −4 |

The entire combined regression at top_200 is in **temporal** (judge noise on 4 of 86 questions).

**At top_50 — the answerer's actual operating point — M30-R3 is the arc peak:**

| Bench | M30-R3 @ top_50 | M28-M29 @ top_50 | Δ |
|---|---|---|---|
| LME | **25/30 (83.3%)** | 22/30 (73.3%) | **+3** |
| LoCoMo | **168/200 (84.0%)** | 167/200 (83.5%) | **+1** |
| **Combined** | **193/230 (83.9%)** | 189/230 (82.2%) | **+4q** |

L1+L2 produced **slightly better candidate ordering** for the cross-encoder reranker — when capped at top_50 (where the answerer is sharpest), this translates to +4q. At top_200 the answerer drowns in extra context, and the gain is invisible under noise.

## 4. Top_50 reporting shift — banked insight

We've been reporting bench scores at top_200 since M14 because the cutoff is configurable in the harness and 200 was set as the most-permissive default. **The model peaks at top_50**:

| Cutoff | M30-R3 LoCoMo | M30-R3 LME |
|---|---|---|
| top_10 | 166 (83.0%) | 23 (76.7%) |
| top_20 | 165 (82.5%) | 21 (70.0%) |
| **top_50** | **168 (84.0%)** | **25 (83.3%)** |
| top_200 | 166 (83.0%) | 21 (70.0%) |

LME drops 13pp from top_50 → top_200. The "context dilution" effect is non-monotonic: top_50 contains the answer-bearing memories with high density; top_200 adds 150 lower-relevance candidates that confuse the gpt-4o-mini answerer.

**Going forward** (v0.15.0 onward):
- Headline cutoff for badge + parity row: **top_50**
- Graceful-degradation reference: top_200
- M19a (v0.14.0) re-reported at top_50 in BENCH_PARITY.yaml for apples-to-apples

## 5. Latency reality check

Hypothesis: L1+L2 cuts per-q latency by 1.5–2×.

**Reality:**
- M28-M29 LoCoMo per-conv latency: ~58s/q
- M30-R3 LoCoMo per-conv latency: ~55-58s/q

**Unchanged.** The parallelization wins (~3-5s saved on retrieval orchestration) are swamped by the answerer LLM call (~10-20s) and the cross-encoder rerank (~1-2s on CPU). The retrieval portion of a question's budget was already ~2s — small absolute savings.

This is a useful negative result: **future latency cycles should target the answerer LLM call directly**, not retrieval orchestration. Levers:
- Shorter prompt (M28-B added confidence + dual-timestamp lines; could trim if needed)
- Smaller model on easy questions (route via question-classifier)
- Speculative decoding or `reasoning_effort=low` for trivial questions
- Batched cross-encoder calls (currently per-question)

## 6. Ship decision — SHIPPED in v0.15.0 consolidated cut

L1 + L2 are safe-by-construction (per-branch exception isolation; identical contract to the prior sequential code). They land in v0.15.0 alongside M21 + M22-M27 + M28-M29.

**Why ship despite zero latency win:**
- Quality positive at top_50 (+4q combined — arc peak)
- Quality neutral at top_200 (−5q within noise)
- Safe by construction; can be reverted with a 6-line diff if needed
- Pattern (`asyncio.gather` over independent retrieval branches) is the right architecture for the production `Astrocyte.search()` path once we wire it through — bench harness validates the pattern

See [`v0.15.0-ship-decision.md`](v0.15.0-ship-decision.md) for the consolidated rationale.

## 7. Carry-forward to M30b

1. **LME measurement cadence — adopt n=90 as standing default.** Prior `n=30` had ±3-6q single-judge noise (proven unmeasurable). Use `MEM0_HARNESS_LME_PER_TYPE=15` (n=90, ~50 min, ~$12/run, ±3pp noise floor) for cycle-close benches. Reserve `=30` (n=180, ~3-4 hours, ~$50-80) for high-confidence pre-release validation only. See [`v0.15.0-ship-decision.md` §5](v0.15.0-ship-decision.md) "LME measurement cadence" for the full table — LME ingestion is per-question (no amortization), so wall-time / cost scales linearly with n, unlike LoCoMo.
2. **Bench-harness state-pollution fix** — auto-delete `predicted_*/` when `bench-runner-deps` re-resets the DB, OR verify ingestion markers against an actual DB query before honoring them.
3. **Answerer-call latency** — the real per-q latency lever (see §5).
4. **Production `Astrocyte.search()` parallelization** — extend L1's `asyncio.gather` pattern from the bench client to the production search method. Bench-only changes don't ship as user-facing speedup; production wiring is the bridge.
5. **Confidence-aware abstention** — wire the M28-B confidence rendering into the answerer prompt (hedge or abstain when confidence < 0.5). Could lift LME single-session-preference (40% → 60%+) and multi-session ceilings.
6. **Temporal-resolution at retain** (M31 candidate) — resolve "last week" / "3 days ago" to absolute dates at retain time so the answerer doesn't have to do date math at query time. M28-B's dual-timestamp rendering is the prerequisite; this cycle activates the consumption side.
